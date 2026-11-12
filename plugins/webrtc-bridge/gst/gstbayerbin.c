/* bayerbin: an 8-bit Bayer mosaic straight to a half-resolution RGBx frame.
 *
 * The preview of a 5 MP Bayer camera is nearly always scaled down (CAM_WEBRTC_MAX_SIZE), and the
 * bridge paid for a full-resolution debayer first: bayer2rgb interpolates 5 M pixels into a 20 MB
 * frame that videoscale then throws three quarters of away -- about 21 ms a frame on an Orin.
 * Each 2x2 cell of the mosaic already IS one RGB pixel: its red, its blue and the mean of its two
 * greens. Nothing is interpolated, so this is one pass over the input.
 *
 * The picture is not identical to bayer2rgb-then-scale (no interpolation; the colour sites sit half
 * a pixel apart inside a cell). bridge_stream.py splices it in only where the stream is being
 * reduced to about half size or less anyway (format_adapt.bin_plan).
 *
 * A first version of this in numpy cost 24.5 ms a frame in the live bridge -- more than what it
 * replaced -- which is why it is compiled.
 *
 *   video/x-bayer,format={rggb,bggr,grbg,gbrg},width=W,height=H
 *     -> video/x-raw,format=RGBx,width=W/2,height=H/2      (odd edges are cropped)
 *
 * Input rows: GStreamer pads video/x-bayer rows to four bytes; the camera core sends tight rows.
 * The buffer size says which.
 */
#include <string.h>
#include <gst/gst.h>
#include <gst/base/gstbasetransform.h>

#define GST_TYPE_BAYER_BIN (gst_bayer_bin_get_type())
G_DECLARE_FINAL_TYPE(GstBayerBin, gst_bayer_bin, GST, BAYER_BIN, GstBaseTransform)

struct _GstBayerBin {
  GstBaseTransform parent;
  gint width, height;        /* of the mosaic */
  gint r_row, r_col;         /* where the cell holds red; blue is diagonally opposite */
};

G_DEFINE_TYPE(GstBayerBin, gst_bayer_bin, GST_TYPE_BASE_TRANSFORM)

#define SINK_CAPS "video/x-bayer, format=(string){ rggb, bggr, grbg, gbrg }, " \
  "width=(int)[ 2, MAX ], height=(int)[ 2, MAX ], framerate=(fraction)[ 0/1, MAX ]"
#define SRC_CAPS "video/x-raw, format=(string)RGBx, " \
  "width=(int)[ 1, MAX ], height=(int)[ 1, MAX ], framerate=(fraction)[ 0/1, MAX ]"

static GstStaticPadTemplate sink_template =
    GST_STATIC_PAD_TEMPLATE("sink", GST_PAD_SINK, GST_PAD_ALWAYS, GST_STATIC_CAPS(SINK_CAPS));
static GstStaticPadTemplate src_template =
    GST_STATIC_PAD_TEMPLATE("src", GST_PAD_SRC, GST_PAD_ALWAYS, GST_STATIC_CAPS(SRC_CAPS));

/* width/height of one side from the other: halved going downstream, a [2n, 2n+1] range going up */
static void scale_dimension(GstStructure* out, const GstStructure* in, const gchar* field, gboolean to_src) {
  const GValue* v = gst_structure_get_value(in, field);
  if (v == NULL) return;
  if (G_VALUE_HOLDS_INT(v)) {
    gint n = g_value_get_int(v);
    if (to_src) gst_structure_set(out, field, G_TYPE_INT, MAX(n / 2, 1), NULL);
    else gst_structure_set(out, field, GST_TYPE_INT_RANGE, 2 * n, 2 * n + 1, NULL);
  } else if (GST_VALUE_HOLDS_INT_RANGE(v)) {
    gint lo = gst_value_get_int_range_min(v), hi = gst_value_get_int_range_max(v);
    if (to_src) gst_structure_set(out, field, GST_TYPE_INT_RANGE, MAX(lo / 2, 1), MAX(hi / 2, 1), NULL);
    else gst_structure_set(out, field, GST_TYPE_INT_RANGE, (gint) MIN((gint64) 2 * lo, G_MAXINT - 1),
                           (gint) MIN((gint64) 2 * hi + 1, G_MAXINT), NULL);
  }
}

static GstCaps* gst_bayer_bin_transform_caps(GstBaseTransform* trans, GstPadDirection direction,
                                             GstCaps* caps, GstCaps* filter) {
  const gboolean to_src = (direction == GST_PAD_SINK);
  GstCaps* result = gst_caps_new_empty();
  for (guint i = 0; i < gst_caps_get_size(caps); i++) {
    const GstStructure* in = gst_caps_get_structure(caps, i);
    GstStructure* out = to_src
        ? gst_structure_new("video/x-raw", "format", G_TYPE_STRING, "RGBx", NULL)
        : gst_structure_new_empty("video/x-bayer");
    const GValue* rate = gst_structure_get_value(in, "framerate");
    scale_dimension(out, in, "width", to_src);
    scale_dimension(out, in, "height", to_src);
    if (rate != NULL) gst_structure_set_value(out, "framerate", rate);
    gst_caps_append_structure(result, out);
  }
  if (filter != NULL) {
    GstCaps* filtered = gst_caps_intersect_full(filter, result, GST_CAPS_INTERSECT_FIRST);
    gst_caps_unref(result);
    result = filtered;
  }
  (void) trans;
  return result;
}

static gboolean gst_bayer_bin_set_caps(GstBaseTransform* trans, GstCaps* incaps, GstCaps* outcaps) {
  GstBayerBin* self = GST_BAYER_BIN(trans);
  const GstStructure* s = gst_caps_get_structure(incaps, 0);
  const gchar* format = gst_structure_get_string(s, "format");
  if (format == NULL || !gst_structure_get_int(s, "width", &self->width) ||
      !gst_structure_get_int(s, "height", &self->height))
    return FALSE;
  if (g_str_equal(format, "rggb")) { self->r_row = 0; self->r_col = 0; }
  else if (g_str_equal(format, "bggr")) { self->r_row = 1; self->r_col = 1; }
  else if (g_str_equal(format, "grbg")) { self->r_row = 0; self->r_col = 1; }
  else if (g_str_equal(format, "gbrg")) { self->r_row = 1; self->r_col = 0; }
  else return FALSE;
  (void) outcaps;
  return TRUE;
}

static gboolean gst_bayer_bin_get_unit_size(GstBaseTransform* trans, GstCaps* caps, gsize* size) {
  const GstStructure* s = gst_caps_get_structure(caps, 0);
  gint w, h;
  if (!gst_structure_get_int(s, "width", &w) || !gst_structure_get_int(s, "height", &h)) return FALSE;
  *size = gst_structure_has_name(s, "video/x-raw") ? (gsize) w * h * 4 : (gsize) GST_ROUND_UP_4(w) * h;
  (void) trans;
  return TRUE;
}

/* The tight rows the camera core sends are smaller than the unit size above whenever the width is
 * no multiple of four, so sizes are worked out here instead of by basetransform's division. */
static gboolean gst_bayer_bin_transform_size(GstBaseTransform* trans, GstPadDirection direction,
                                             GstCaps* caps, gsize size, GstCaps* othercaps, gsize* othersize) {
  (void) direction; (void) caps; (void) size;
  return gst_bayer_bin_get_unit_size(trans, othercaps, othersize);
}

static GstFlowReturn gst_bayer_bin_transform(GstBaseTransform* trans, GstBuffer* inbuf, GstBuffer* outbuf) {
  GstBayerBin* self = GST_BAYER_BIN(trans);
  const gint w = self->width, h = self->height, ow = w / 2, oh = h / 2;
  GstMapInfo in, out;
  if (!gst_buffer_map(inbuf, &in, GST_MAP_READ)) return GST_FLOW_ERROR;
  const gsize padded = (gsize) GST_ROUND_UP_4(w);
  const gsize stride = (in.size == padded * (gsize) h) ? padded : (gsize) w;
  if (in.size < stride * (gsize) (h - 1) + (gsize) w) {       /* a torn frame: drop it, stay alive */
    gst_buffer_unmap(inbuf, &in);
    GST_WARNING_OBJECT(self, "%" G_GSIZE_FORMAT " bytes is not a %dx%d mosaic; dropped", in.size, w, h);
    return GST_BASE_TRANSFORM_FLOW_DROPPED;
  }
  if (!gst_buffer_map(outbuf, &out, GST_MAP_WRITE)) {
    gst_buffer_unmap(inbuf, &in);
    return GST_FLOW_ERROR;
  }
  /* within a cell: red at (r_row, r_col), blue diagonally opposite, the greens at the other two */
  const gint rr = self->r_row, rc = self->r_col;
  for (gint y = 0; y < oh; y++) {
    const guint8* red_row = in.data + stride * (gsize) (2 * y + rr);
    const guint8* blue_row = in.data + stride * (gsize) (2 * y + 1 - rr);
    guint8* restrict d = out.data + (gsize) y * ow * 4;
    for (gint x = 0; x < ow; x++) {
      const guint8 r = red_row[2 * x + rc], g1 = red_row[2 * x + 1 - rc];
      const guint8 g2 = blue_row[2 * x + rc], b = blue_row[2 * x + 1 - rc];
      d[4 * x + 0] = r;
      d[4 * x + 1] = (guint8) (((guint) g1 + g2) >> 1);
      d[4 * x + 2] = b;
      d[4 * x + 3] = 255;
    }
  }
  gst_buffer_unmap(outbuf, &out);
  gst_buffer_unmap(inbuf, &in);
  return GST_FLOW_OK;
}

static void gst_bayer_bin_class_init(GstBayerBinClass* klass) {
  GstElementClass* element_class = GST_ELEMENT_CLASS(klass);
  GstBaseTransformClass* transform_class = GST_BASE_TRANSFORM_CLASS(klass);
  gst_element_class_add_static_pad_template(element_class, &sink_template);
  gst_element_class_add_static_pad_template(element_class, &src_template);
  gst_element_class_set_static_metadata(element_class, "Bayer to half-resolution RGB",
      "Filter/Converter/Video", "One RGBx pixel per 2x2 cell of an 8-bit Bayer mosaic (no interpolation)",
      "camera-service");
  transform_class->transform_caps = GST_DEBUG_FUNCPTR(gst_bayer_bin_transform_caps);
  transform_class->set_caps = GST_DEBUG_FUNCPTR(gst_bayer_bin_set_caps);
  transform_class->get_unit_size = GST_DEBUG_FUNCPTR(gst_bayer_bin_get_unit_size);
  transform_class->transform_size = GST_DEBUG_FUNCPTR(gst_bayer_bin_transform_size);
  transform_class->transform = GST_DEBUG_FUNCPTR(gst_bayer_bin_transform);
}

static void gst_bayer_bin_init(GstBayerBin* self) { (void) self; }

static gboolean plugin_init(GstPlugin* plugin) {
  return gst_element_register(plugin, "bayerbin", GST_RANK_NONE, GST_TYPE_BAYER_BIN);
}

#define PACKAGE "camera-service"
GST_PLUGIN_DEFINE(GST_VERSION_MAJOR, GST_VERSION_MINOR, bayerbin,
    "Bayer mosaic to half-resolution RGB", plugin_init, "1.0", "Apache 2.0", "camera-service",
    "camera-service")
