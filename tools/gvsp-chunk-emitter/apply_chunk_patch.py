#!/usr/bin/env python3
"""Patch the Aravis fake GV camera to emit GVSP chunk data (ChunkTimestamp + ChunkFrameID).

The stock fake camera has no chunk support (payload hardcoded to IMAGE), so it can't
exercise a chunk-parsing pipeline. This patch makes it emit, per frame, two GenICam
chunks after the image:

    ChunkTimestamp (ChunkID 0xa001, 8 bytes BE) = the buffer timestamp (system time, ns)
    ChunkFrameID   (ChunkID 0xa002, 8 bytes BE) = the frame id

GenICam chunk layout (parsed backward from received_size, big-endian):
    [image][ts_value:8][0xa001:4][8:4][fid_value:8][0xa002:4][8:4]

It touches 4 files:
  - arvfakecamera.h  : a CHUNK_MODE_ACTIVE register address (0x140)
  - arvfakecamera.c  : get_payload (+32 bytes when chunk mode on) + fill_buffer (emit chunks)
  - arvgvfakecamera.c: flag the GVSP leader as image+chunks (payload_type | 0x4000)
  - arv-fake-camera.xml: ChunkDataControl nodes + chunk-aware PayloadSize

Usage: python3 apply_chunk_patch.py <aravis_src_dir>
"""
import re
import sys

SRC = sys.argv[1] if len(sys.argv) > 1 else "."


def patch(path, replacements):
    full = f"{SRC}/{path}"
    with open(full) as f:
        text = f.read()
    for i, (old, new) in enumerate(replacements):
        if isinstance(old, re.Pattern):
            text, n = old.subn(new, text, count=1)
        else:
            assert old in text, f"{path}: anchor #{i} not found:\n{old[:120]}"
            n, text = 1, text.replace(old, new, 1)
        assert n == 1, f"{path}: replacement #{i} matched {n} times"
    with open(full, "w") as f:
        f.write(text)
    print(f"patched {path} ({len(replacements)} edit(s))")


# --- arvfakecamera.h: chunk-mode register ---
patch("src/arvfakecamera.h", [
    (re.compile(r"(#define ARV_FAKE_CAMERA_REGISTER_TEST\s+0x1f0)"),
     r"\1\n#define ARV_FAKE_CAMERA_REGISTER_CHUNK_MODE_ACTIVE 0x140"),
])

# --- arvfakecamera.c: chunk-aware payload + chunk emission ---
patch("src/arvfakecamera.c", [
    ("#include <string.h>", "#include <string.h>\n#include <stdio.h>"),
    ("return width * height * ARV_PIXEL_FORMAT_BIT_PER_PIXEL(pixel_format)/8;",
     "{\n"
     "\t\tsize_t _payload = width * height * ARV_PIXEL_FORMAT_BIT_PER_PIXEL(pixel_format)/8;\n"
     "\t\tif (_get_register (camera, ARV_FAKE_CAMERA_REGISTER_CHUNK_MODE_ACTIVE) != 0)\n"
     "\t\t\t_payload += 32; /* ChunkTimestamp + ChunkFrameID: 8B value + 8B trailer each */\n"
     "\t\treturn _payload;\n"
     "\t}"),
    ("buffer->priv->parts[0].size = buffer->priv->received_size;",
     "buffer->priv->parts[0].size = buffer->priv->received_size;\n\n"
     "\t/* replay patch: override image + timestamp from files (env ARV_REPLAY_FRAMES/TIMESTAMPS) */\n"
     "\t{\n"
     "\t\tstatic FILE *_rf = NULL; static FILE *_tf = NULL; static int _rinit = 0;\n"
     "\t\tif (!_rinit) {\n"
     "\t\t\tconst char *_fp = g_getenv (\"ARV_REPLAY_FRAMES\");\n"
     "\t\t\tconst char *_tp = g_getenv (\"ARV_REPLAY_TIMESTAMPS\");\n"
     "\t\t\tif (_fp != NULL && _tp != NULL) { _rf = fopen (_fp, \"rb\"); _tf = fopen (_tp, \"r\"); }\n"
     "\t\t\t_rinit = 1;\n"
     "\t\t}\n"
     "\t\tif (_rf != NULL && _tf != NULL) {\n"
     "\t\t\tsize_t _img = buffer->priv->received_size; size_t _n;\n"
     "\t\t\tchar _line[64]; char *_r;\n"
     "\t\t\t_n = fread (buffer->priv->data, 1, _img, _rf);\n"
     "\t\t\tif (_n != _img) { rewind (_rf); _n = fread (buffer->priv->data, 1, _img, _rf); }\n"
     "\t\t\t(void) _n;\n"
     "\t\t\t_r = fgets (_line, sizeof (_line), _tf);\n"
     "\t\t\tif (_r == NULL) { rewind (_tf); _r = fgets (_line, sizeof (_line), _tf); }\n"
     "\t\t\tif (_r != NULL) {\n"
     "\t\t\t\tbuffer->priv->timestamp_ns = g_ascii_strtoull (_line, NULL, 10);\n"
     "\t\t\t\tbuffer->priv->system_timestamp_ns = buffer->priv->timestamp_ns;\n"
     "\t\t\t}\n"
     "\t\t}\n"
     "\t}\n\n"
     "\t/* chunk-emitter patch: append ChunkTimestamp(0xa001) + ChunkFrameID(0xa002) */\n"
     "\tif (_get_register (camera, ARV_FAKE_CAMERA_REGISTER_CHUNK_MODE_ACTIVE) != 0 &&\n"
     "\t    buffer->priv->received_size + 32 <= buffer->priv->allocated_size) {\n"
     "\t\tguint8 *cdata = (guint8 *) buffer->priv->data;\n"
     "\t\tsize_t coff = buffer->priv->received_size;\n"
     "\t\tguint64 ts_be = GUINT64_TO_BE (buffer->priv->timestamp_ns);\n"
     "\t\tguint64 fid_be = GUINT64_TO_BE ((guint64) buffer->priv->frame_id);\n"
     "\t\tguint32 id_be, size_be = GUINT32_TO_BE (8);\n"
     "\t\tmemcpy (cdata + coff, &ts_be, 8); coff += 8;\n"
     "\t\tid_be = GUINT32_TO_BE (0xa001); memcpy (cdata + coff, &id_be, 4); coff += 4;\n"
     "\t\tmemcpy (cdata + coff, &size_be, 4); coff += 4;\n"
     "\t\tmemcpy (cdata + coff, &fid_be, 8); coff += 8;\n"
     "\t\tid_be = GUINT32_TO_BE (0xa002); memcpy (cdata + coff, &id_be, 4); coff += 4;\n"
     "\t\tmemcpy (cdata + coff, &size_be, 4); coff += 4;\n"
     "\t\tbuffer->priv->received_size = coff;\n"
     "\t\tbuffer->priv->payload_type = ARV_BUFFER_PAYLOAD_TYPE_EXTENDED_CHUNK_DATA;\n"
     "\t}"),
])

# --- arvgvfakecamera.c: flag the GVSP leader as image+chunks ---
patch("src/arvgvfakecamera.c", [
    ("                                                                  0, 0,\n"
     "                                                                  packet_buffer, &packet_size);",
     "                                                                  0, 0,\n"
     "                                                                  packet_buffer, &packet_size);\n\n"
     "\t\t\t\t/* chunk-emitter patch: mark leader as image+chunks (0x4000) */\n"
     "\t\t\t\tif (arv_buffer_get_payload_type (image_buffer) ==\n"
     "\t\t\t\t    ARV_BUFFER_PAYLOAD_TYPE_EXTENDED_CHUNK_DATA) {\n"
     "\t\t\t\t\tArvGvspImageLeader *_leader = (ArvGvspImageLeader *)\n"
     "\t\t\t\t\t\tarv_gvsp_packet_get_data ((ArvGvspPacket *) packet_buffer);\n"
     "\t\t\t\t\tif (_leader != NULL)\n"
     "\t\t\t\t\t\t_leader->payload_type = g_htons (ARV_BUFFER_PAYLOAD_TYPE_IMAGE | 0x4000);\n"
     "\t\t\t\t}"),
])

# --- arv-fake-camera.xml: chunk nodes + chunk-aware PayloadSize ---
CHUNK_XML = """\t<!-- Chunk data control (chunk-emitter patch) -->

\t<Category Name="ChunkDataControl" NameSpace="Standard">
\t\t<pFeature>ChunkModeActive</pFeature>
\t\t<pFeature>ChunkSelector</pFeature>
\t\t<pFeature>ChunkEnable</pFeature>
\t\t<pFeature>ChunkTimestamp</pFeature>
\t\t<pFeature>ChunkFrameID</pFeature>
\t</Category>

\t<Boolean Name="ChunkModeActive" NameSpace="Standard">
\t\t<pValue>ChunkModeActiveRegister</pValue><OnValue>1</OnValue><OffValue>0</OffValue>
\t</Boolean>
\t<IntReg Name="ChunkModeActiveRegister" NameSpace="Custom">
\t\t<Address>0x140</Address><Length>4</Length><AccessMode>RW</AccessMode>
\t\t<pPort>Device</pPort><Sign>Unsigned</Sign><Endianess>BigEndian</Endianess>
\t</IntReg>

\t<Enumeration Name="ChunkSelector" NameSpace="Standard">
\t\t<EnumEntry Name="Timestamp" NameSpace="Standard"><Value>0</Value></EnumEntry>
\t\t<EnumEntry Name="FrameID" NameSpace="Standard"><Value>1</Value></EnumEntry>
\t\t<pValue>ChunkSelectorRegister</pValue>
\t</Enumeration>
\t<IntReg Name="ChunkSelectorRegister" NameSpace="Custom">
\t\t<Address>0x144</Address><Length>4</Length><AccessMode>RW</AccessMode>
\t\t<pPort>Device</pPort><Sign>Unsigned</Sign><Endianess>BigEndian</Endianess>
\t</IntReg>

\t<Boolean Name="ChunkEnable" NameSpace="Standard">
\t\t<pValue>ChunkEnableRegister</pValue><OnValue>1</OnValue><OffValue>0</OffValue>
\t</Boolean>
\t<IntReg Name="ChunkEnableRegister" NameSpace="Custom">
\t\t<Address>0x148</Address><Length>4</Length><AccessMode>RW</AccessMode>
\t\t<pPort>Device</pPort><Sign>Unsigned</Sign><Endianess>BigEndian</Endianess>
\t</IntReg>

\t<IntReg Name="ChunkTimestamp" NameSpace="Standard">
\t\t<Address>0x00</Address><Length>8</Length><AccessMode>RO</AccessMode>
\t\t<pPort>ChunkTimestampPort</pPort><Cachable>NoCache</Cachable>
\t\t<Sign>Unsigned</Sign><Endianess>BigEndian</Endianess>
\t</IntReg>
\t<Port Name="ChunkTimestampPort" NameSpace="Custom"><ChunkID>0000a001</ChunkID></Port>

\t<IntReg Name="ChunkFrameID" NameSpace="Standard">
\t\t<Address>0x00</Address><Length>8</Length><AccessMode>RO</AccessMode>
\t\t<pPort>ChunkFrameIDPort</pPort><Cachable>NoCache</Cachable>
\t\t<Sign>Unsigned</Sign><Endianess>BigEndian</Endianess>
\t</IntReg>
\t<Port Name="ChunkFrameIDPort" NameSpace="Custom"><ChunkID>0000a002</ChunkID></Port>

"""
patch("src/arv-fake-camera.xml", [
    ("\t\t<pFeature>Debug</pFeature>",
     "\t\t<pFeature>ChunkDataControl</pFeature>\n\t\t<pFeature>Debug</pFeature>"),
    ('<pVariable Name="PIXELFORMAT">PixelFormatRegister</pVariable>',
     '<pVariable Name="PIXELFORMAT">PixelFormatRegister</pVariable>\n'
     '\t\t<pVariable Name="CHUNKMODE">ChunkModeActiveRegister</pVariable>'),
    ("<Formula>WIDTH * HEIGHT * ((PIXELFORMAT>>16)&amp;0xFF) / 8</Formula>",
     "<Formula>WIDTH * HEIGHT * ((PIXELFORMAT>>16)&amp;0xFF) / 8 + CHUNKMODE * 32</Formula>"),
    ("\t<!-- Port -->", CHUNK_XML + "\t<!-- Port -->"),
])

print("chunk patch applied OK")
