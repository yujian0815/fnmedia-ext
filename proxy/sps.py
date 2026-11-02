#!/usr/bin/env python3
"""H.264 SPS 解析 —— 从 TS/fMP4 分片头字节读出画面宽高与帧率。

用途：识别"重新编码拼接"进正片的广告。采集站把广告按 ``#EXT-X-DISCONTINUITY``
整块拼进播放列表时，列表里没有任何广告痕迹（无 SCTE-35、分片是同 CDN 随机哈希、
时长与正文同量级），唯一稳定特征是**编码参数**：广告通常由另一套转码流程产出，
分辨率/帧率与正文不同（实测 1920x800@25fps 正文 vs 1920x1080@30fps 广告）。

实现移植自 MoonTV 的 segment-media-probe（MIT，TypeScript），纯标准库：
  - BitReader：H.264 的 ue(v)/se(v) 指数哥伦布变长码；位宽累乘避免 32 位溢出；
  - parse_sps_profile：解析一个 SPS NAL，返回 {width,height,fps?}；
  - parse_annexb_profile：在 Annex-B（TS）字节流里找第一个 SPS；
  - parse_avcc_profile：在 fMP4 的 avcC（AVCDecoderConfigurationRecord）里找 SPS；
  - parse_media_profile_head：先 avcC 后 Annex-B。

只读分片头几 KB（实测 SPS 位于分片第 0.6~1.4KB），任何解析失败返回 None，
调用方据此"不做判定"，绝不因探测失败误删正文。
"""
from __future__ import annotations

from typing import Optional

_HIGH_PROFILE_IDCS = frozenset(
    (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135))


class _BitReader:
    """SPS 按位读取器。read_bits 用 int 累加（Python 任意精度，无溢出）。"""
    __slots__ = ("b", "p", "n")

    def __init__(self, data: bytes):
        self.b = data
        self.p = 0
        self.n = len(data) * 8

    def bit(self) -> int:
        if self.p >= self.n:
            raise ValueError("SPS out of range")
        byte = self.b[self.p >> 3]
        v = (byte >> (7 - (self.p & 7))) & 1
        self.p += 1
        return v

    def bits(self, count: int) -> int:
        v = 0
        for _ in range(count):
            v = v * 2 + self.bit()
        return v

    def ue(self) -> int:
        zeros = 0
        while self.bit() == 0:
            zeros += 1
            if zeros > 31:
                raise ValueError("SPS ue(v) invalid")
        return (1 << zeros) - 1 + (self.bits(zeros) if zeros else 0)

    def se(self) -> int:
        code = self.ue()
        mag = (code + 1) // 2
        return mag if code % 2 == 1 else -mag


def _skip_scaling_list(r: _BitReader, size: int) -> None:
    last = 8
    nxt = 8
    for _ in range(size):
        if nxt != 0:
            nxt = (last + r.se() + 256) % 256
        if nxt != 0:
            last = nxt


def _strip_emulation_prevention(payload: bytes) -> bytes:
    """去掉防竞争字节 00 00 03 → 00 00（RBSP 还原）。"""
    out = bytearray()
    zeros = 0
    for byte in payload:
        if zeros >= 2 and byte == 0x03:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def parse_sps_profile(nal: bytes) -> Optional[dict]:
    """解析一个 H.264 SPS NAL，返回 {"width","height","fps"?}，失败返回 None。

    nal 可带 NAL 头（首字节低 5 位 == 7），也可直接是 SPS 的 RBSP。
    """
    if not nal or len(nal) < 5:
        return None
    payload = nal[1:] if (nal[0] & 0x1F) == 7 else nal
    rbsp = _strip_emulation_prevention(payload)
    try:
        r = _BitReader(rbsp)
        profile_idc = r.bits(8)
        r.bits(8)  # constraint flags
        r.bits(8)  # level_idc
        r.ue()    # seq_parameter_set_id

        chroma_format_idc = 1
        separate_colour_plane = 0
        if profile_idc in _HIGH_PROFILE_IDCS:
            chroma_format_idc = r.ue()
            if chroma_format_idc == 3:
                separate_colour_plane = r.bit()
            r.ue()  # bit_depth_luma_minus8
            r.ue()  # bit_depth_chroma_minus8
            r.bit()  # qpprime_y_zero_transform_bypass_flag
            if r.bit() == 1:  # seq_scaling_matrix_present_flag
                lists = 8 if chroma_format_idc != 3 else 12
                for i in range(lists):
                    if r.bit() == 1:
                        _skip_scaling_list(r, 16 if i < 6 else 64)

        r.ue()  # log2_max_frame_num_minus4
        poc_type = r.ue()
        if poc_type == 0:
            r.ue()  # log2_max_pic_order_cnt_lsb_minus4
        elif poc_type == 1:
            r.bit()  # delta_pic_order_always_zero_flag
            r.se()   # offset_for_non_ref_pic
            r.se()   # offset_for_top_to_bottom_field
            cycle = r.ue()
            for _ in range(cycle):
                r.se()

        r.ue()   # max_num_ref_frames
        r.bit()  # gaps_in_frame_num_value_allowed_flag
        width_in_mbs = r.ue() + 1
        height_in_map_units = r.ue() + 1
        frame_mbs_only = r.bit()
        if frame_mbs_only == 0:
            r.bit()  # mb_adaptive_frame_field_flag
        r.bit()  # direct_8x8_inference_flag

        cl = cr = ct = cb = 0
        if r.bit() == 1:  # frame_cropping_flag
            cl, cr, ct, cb = r.ue(), r.ue(), r.ue(), r.ue()

        fps = None
        if r.bit() == 1:  # vui_parameters_present_flag
            if r.bit() == 1:  # aspect_ratio_info_present_flag
                if r.bits(8) == 255:  # Extended_SAR
                    r.bits(16)
                    r.bits(16)
            if r.bit() == 1:  # overscan_info_present_flag
                r.bit()
            if r.bit() == 1:  # video_signal_type_present_flag
                r.bits(3)
                r.bit()
                if r.bit() == 1:  # colour_description_present_flag
                    r.bits(8)
                    r.bits(8)
                    r.bits(8)
            if r.bit() == 1:  # chroma_loc_info_present_flag
                r.ue()
                r.ue()
            if r.bit() == 1:  # timing_info_present_flag
                num_units = r.bits(32)
                time_scale = r.bits(32)
                if num_units > 0 and time_scale > 0:
                    val = time_scale / (2 * num_units)
                    if 0 < val < 1000:
                        fps = round(val, 2)

        chroma_array_type = 0 if separate_colour_plane else chroma_format_idc
        sub_w = 2 if chroma_array_type in (1, 2) else 1
        sub_h = 2 if chroma_array_type == 1 else 1
        crop_x = 1 if chroma_array_type == 0 else sub_w
        crop_y = ((2 - frame_mbs_only) if chroma_array_type == 0
                  else sub_h * (2 - frame_mbs_only))

        width = width_in_mbs * 16 - (cl + cr) * crop_x
        height = (2 - frame_mbs_only) * height_in_map_units * 16 \
            - (ct + cb) * crop_y
        if width <= 0 or height <= 0:
            return None
        return {"width": width, "height": height, "fps": fps} if fps else \
            {"width": width, "height": height}
    except Exception:
        return None


def _find_start_code(data: bytes, start: int) -> int:
    """从 start 起找下一个 Annex-B 起始码开头位置（含可选前导 00），找不到 -1。"""
    i = start
    n = len(data)
    while i + 3 <= n:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            return i - 1 if i > 0 and data[i - 1] == 0 else i
        i += 1
    return -1


def parse_annexb_profile(data: bytes) -> Optional[dict]:
    """在 Annex-B（TS 分片）字节流里找第一个 SPS NAL 并解析。"""
    n = len(data)
    i = 2
    while i + 4 <= n:
        if data[i] == 0x01 and data[i - 1] == 0 and data[i - 2] == 0:
            header_at = i + 1
            if (data[header_at] & 0x1F) == 7:
                nxt = _find_start_code(data, header_at + 1)
                end = n if nxt == -1 else nxt
                prof = parse_sps_profile(data[header_at:end])
                if prof:
                    return prof
        i += 1
    return None


def parse_avcc_profile(data: bytes) -> Optional[dict]:
    """从 fMP4 的 avcC（'avcC' 四字符）里解析 SPS 尺寸。"""
    n = len(data)
    i = 0
    while i + 12 <= n:
        if data[i:i + 4] == b"avcC":
            sps_count = data[i + 9] & 0x1F
            off = i + 10
            for _ in range(sps_count):
                if off + 2 > n:
                    return None
                length = (data[off] << 8) | data[off + 1]
                off += 2
                if length <= 0 or off + length > n:
                    return None
                prof = parse_sps_profile(data[off:off + length])
                if prof:
                    return prof
                off += length
            return None
        i += 1
    return None


def parse_media_profile_head(data) -> Optional[dict]:
    """从分片头字节解析编码指纹：先 fMP4 avcC，再 Annex-B（TS）。"""
    if not data:
        return None
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return None
    b = bytes(data)
    return parse_avcc_profile(b) or parse_annexb_profile(b)


def profile_key(p: dict) -> str:
    size = f"{p['width']}x{p['height']}"
    return f"{size}@{p['fps']}fps" if p.get("fps") else size


def same_profile(a: dict, b: dict) -> bool:
    """两套编码是否一致：宽高必须一致；帧率仅双方都有时才比较（容差 1.5fps）。"""
    if a["width"] != b["width"] or a["height"] != b["height"]:
        return False
    fa, fb = a.get("fps"), b.get("fps")
    if not fa or not fb:
        return True
    return abs(fa - fb) < 1.5
