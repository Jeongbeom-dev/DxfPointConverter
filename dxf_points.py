# -*- coding: utf-8 -*-
"""
dxf_points.py
-------------
AutoCAD DXF(ASCII) 파일을 X, Y 포인트 리스트로 변환하는 모듈.

- 외부 라이브러리 불필요 (표준 라이브러리만 사용)
- 지원 엔티티: LINE, LWPOLYLINE, POLYLINE, CIRCLE, ARC, POINT
- 원/호/곡선은 일정 구간(seg_len) 단위로 직선 포인트로 분할
- G-code 명령어 없이 순수 (x, y) 포인트만 반환

핵심 함수:
    dxf_to_paths(path, seg_len) -> [ path0, path1, ... ]
        각 path 는 [(x, y), (x, y), ...] 포인트 리스트 (한 번에 그리는 하나의 경로)
"""

import math


# 하나의 호/원을 분할할 때 생성할 최대 포인트 수 (과도 분할/메모리 폭주 방지)
_MAX_ARC_POINTS = 100000

# 면채우기 스캔라인 최대 개수 (최종 안전장치 — 호출부에서 사전 검증 권장)
_MAX_INFILL_LINES = 200000

# ENTITIES 로 인식하면 안 되는 구조/테이블 토큰 (미지원 통계에서 제외)
_STRUCTURAL = {
    "SECTION", "ENDSEC", "TABLE", "ENDTAB", "LAYER", "EOF",
    "BLOCK", "ENDBLK", "VPORT", "LTYPE", "STYLE", "APPID",
    "DIMSTYLE", "UCS", "VIEW", "CLASS", "BLOCK_RECORD", "HEADER",
}


# ---------------------------------------------------------------------------
# DXF 파일 읽기
# ---------------------------------------------------------------------------
def _read_pairs(path):
    """DXF 파일을 (group_code, value) 쌍 리스트로 읽는다."""
    encodings = ("utf-8", "cp949", "latin-1")
    lines = None
    for enc in encodings:
        try:
            with open(path, "r", encoding=enc) as f:
                lines = [ln.rstrip("\r\n") for ln in f]
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if lines is None:
        raise IOError(f"파일을 읽을 수 없습니다: {path}")

    pairs = []
    i, n = 0, len(lines)
    while i + 1 < n:
        code_str = lines[i].strip()
        value = lines[i + 1]
        i += 2
        if code_str == "":
            continue
        try:
            code = int(code_str)
        except ValueError:
            continue
        pairs.append((code, value))
    return pairs


def _parse_entities(pairs):
    """ENTITIES 섹션의 엔티티를 dict 리스트로 추출한다."""
    start, end = None, None
    for idx, (code, value) in enumerate(pairs):
        v = value.strip()
        if code == 2 and v == "ENTITIES":
            start = idx + 1
        elif code == 0 and v == "ENDSEC" and start is not None:
            end = idx
            break
    if start is None:
        start, end = 0, len(pairs)
    if end is None:
        end = len(pairs)

    entities = []
    current = None
    for code, value in pairs[start:end]:
        if code == 0:
            if current is not None:
                entities.append(current)
            # _raw: 그룹코드가 나온 순서를 그대로 보존 (LWPOLYLINE bulge 연결에 필요)
            current = {"type": value.strip(), "_raw": []}
        else:
            if current is None:
                continue
            current.setdefault(code, []).append(value)
            current["_raw"].append((code, value))
    if current is not None:
        entities.append(current)
    return entities


def _f(ent, code, index=0, default=0.0):
    try:
        return float(ent[code][index])
    except (KeyError, IndexError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 곡선 -> 포인트 분할
# ---------------------------------------------------------------------------
def _arc_points(cx, cy, r, start_deg, end_deg, seg_len, ccw=True):
    """호를 일정 구간(seg_len) 길이의 직선 포인트로 분할한다.

    start_deg == end_deg 는 '스윕 0'(빈 호, 점 1개)으로 처리한다.
    완전한 원은 CIRCLE 이 0~360 을 직접 넘겨 처리하므로 여기서는
    동일 각도를 360 도로 부풀리지 않는다 (부등호를 < / > 로 사용).
    """
    start = math.radians(start_deg)
    end = math.radians(end_deg)
    if ccw:
        while end < start:
            end += 2 * math.pi
        sweep = end - start
    else:
        while end > start:
            end -= 2 * math.pi
        sweep = start - end
    if abs(sweep) < 1e-12:
        return [(cx + r * math.cos(start), cy + r * math.sin(start))]
    arc_len = abs(sweep) * r
    n = max(2, int(math.ceil(arc_len / max(seg_len, 1e-6))))
    n = max(n, int(math.ceil(abs(sweep) / math.radians(10))))  # 최소 10도 간격
    n = min(n, _MAX_ARC_POINTS)                                # 과도 분할 상한
    pts = []
    for i in range(n + 1):
        t = i / n
        ang = start + (sweep if ccw else -sweep) * t
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return pts


def _bulge_points(p1, p2, bulge, seg_len):
    """LWPOLYLINE/POLYLINE bulge(볼록도)를 원호 포인트로 변환한다 (p1 제외, p2 포함).

    포함각 theta(=4·atan(bulge), CCW 양수)로 시작점에서 직접 스윕한다.
    끝점을 atan2 로 재계산해 스윕 방향을 추정하지 않으므로, 180°를 넘는
    장호(major arc)와 시계/반시계 방향이 모두 정확히 처리된다.
    """
    if abs(bulge) < 1e-12:
        return [p2]
    x1, y1 = p1
    x2, y2 = p2
    chord = math.hypot(x2 - x1, y2 - y1)
    if chord < 1e-12:
        return [p2]
    theta = 4 * math.atan(bulge)             # 부호 포함 포함각
    half = chord / 2.0
    r = abs(half / math.sin(theta / 2.0))    # 반지름(양수)
    t = half / math.tan(theta / 2.0)         # 현 중점 -> 중심 (부호 있는 apothem)
    ux, uy = (x2 - x1) / chord, (y2 - y1) / chord
    nx, ny = -uy, ux                         # 좌수직 법선
    cx = (x1 + x2) / 2.0 + nx * t
    cy = (y1 + y2) / 2.0 + ny * t
    sa = math.atan2(y1 - cy, x1 - cx)        # 시작 각(라디안)
    arc_len = abs(theta) * r
    n = max(2, int(math.ceil(arc_len / max(seg_len, 1e-6))))
    n = max(n, int(math.ceil(abs(theta) / math.radians(10))))  # 최소 10도 간격
    n = min(n, _MAX_ARC_POINTS)
    pts = [(cx + r * math.cos(sa + theta * (i / n)),
            cy + r * math.sin(sa + theta * (i / n))) for i in range(1, n + 1)]
    pts[-1] = (x2, y2)                        # 끝점 부동소수 오차 스냅
    return pts


def _polyline_points(verts, closed, seg_len):
    """정점 리스트를 포인트 경로로 변환한다.
    verts: [(x, y, bulge), ...]  — 각 bulge 는 '그 정점 -> 다음 정점' 세그먼트에 적용.
    """
    if not verts:
        return []
    m = len(verts)
    pts = [(verts[0][0], verts[0][1])]
    last = m if closed else m - 1     # 닫힌 폴리라인은 마지막->첫 정점 세그먼트 포함
    for i in range(last):
        x1, y1, b = verts[i]
        x2, y2, _b2 = verts[(i + 1) % m]
        if abs(b) > 1e-12:
            pts.extend(_bulge_points((x1, y1), (x2, y2), b, seg_len))
        else:
            pts.append((x2, y2))
    return pts


def _lwpoly_verts(ent):
    """LWPOLYLINE 정점을 (x, y, bulge) 순서대로 재구성한다.
    DXF 는 정점마다 code 42(bulge)를 '선택적으로' 내보내므로(곡선 세그먼트에만),
    _raw(그룹코드 등장 순서)를 이용해 각 bulge 를 정확한 정점에 연결한다.
    """
    verts = []
    raw = ent.get("_raw")
    if raw:
        cx = cy = 0.0
        cb = 0.0
        started = False
        for code, value in raw:
            if code == 10:                 # 새 정점 시작
                if started:
                    verts.append((cx, cy, cb))
                try:
                    cx = float(value)
                except ValueError:
                    cx = 0.0
                cy, cb = 0.0, 0.0
                started = True
            elif code == 20 and started:
                try:
                    cy = float(value)
                except ValueError:
                    cy = 0.0
            elif code == 42 and started:
                try:
                    cb = float(value)
                except ValueError:
                    cb = 0.0
        if started:
            verts.append((cx, cy, cb))
    if not verts:
        # 폴백: code 10/20 리스트만 사용 (bulge 없음)
        xs, ys = ent.get(10, []), ent.get(20, [])
        for i in range(min(len(xs), len(ys))):
            try:
                verts.append((float(xs[i]), float(ys[i]), 0.0))
            except ValueError:
                pass
    return verts


# ---------------------------------------------------------------------------
# 엔티티 -> 경로(포인트 리스트)
# ---------------------------------------------------------------------------
def _bspline_points(ctrl, knots, degree, seg_len):
    """B-스플라인을 de Boor 알고리즘으로 샘플링해 포인트 리스트로 반환한다.
    ctrl: [(x,y),...] 제어점, knots: 노트벡터, degree: 차수.
    """
    m = len(ctrl)
    p = degree
    if m < 2 or p < 1 or len(knots) < m + p + 1:
        return list(ctrl)
    u0, u1 = knots[p], knots[m]              # 유효 파라미터 구간 [u_p, u_m]
    if u1 <= u0:
        return list(ctrl)

    def deboor(u):
        # u 를 포함하는 노트 구간 k 찾기 (u_k <= u < u_{k+1}), 유효구간으로 클램프
        if u >= knots[m]:
            k = m - 1
        elif u <= knots[p]:
            k = p
        else:
            k = p
            while k < m - 1 and not (knots[k] <= u < knots[k + 1]):
                k += 1
        d = [ctrl[j + k - p] for j in range(p + 1)]
        for r in range(1, p + 1):
            for j in range(p, r - 1, -1):
                idx = j + k - p
                denom = knots[idx + p - r + 1] - knots[idx]
                a = 0.0 if abs(denom) < 1e-12 else (u - knots[idx]) / denom
                d[j] = ((1 - a) * d[j - 1][0] + a * d[j][0],
                        (1 - a) * d[j - 1][1] + a * d[j][1])
        return d[p]

    # 대략적 곡선 길이로 샘플 수 결정
    approx_len = sum(math.hypot(ctrl[i + 1][0] - ctrl[i][0],
                                ctrl[i + 1][1] - ctrl[i][1]) for i in range(m - 1))
    n = max(p + 1, int(math.ceil(approx_len / max(seg_len, 1e-6))))
    n = min(n, _MAX_ARC_POINTS)
    return [deboor(u0 + (u1 - u0) * (i / n)) for i in range(n + 1)]


def _read_hatch_spline(raw, i, n, seg_len):
    """HATCH 스플라인 엣지(type 4)를 구조대로 읽어 (근사 포인트, 다음 인덱스) 반환.

    구조: 94 degree, 73 rational, 74 periodic, 95 nknots, 96 nctrl,
          40×nknots (노트), (10,20)×nctrl (+옵션 42×nctrl 가중치),
          97 nfit, (11,21)×nfit (피팅점), [12/22,13/23 탄젠트].
    근사: 피팅점 우선(곡선 위) → de Boor 평가 → 제어점 다각형.
    """
    def rd(code):
        nonlocal i
        if i < n and raw[i][0] == code:
            v = raw[i][1]
            i += 1
            try:
                return float(v)
            except ValueError:
                return 0.0
        return None

    degree = rd(94)
    rd(73)
    rd(74)
    nknots = rd(95)
    nctrl = rd(96)
    degree = int(degree) if degree is not None else 3
    nknots = int(nknots) if nknots is not None else 0
    nctrl = int(nctrl) if nctrl is not None else 0

    knots = []
    for _k in range(nknots):
        v = rd(40)
        if v is None:
            break
        knots.append(v)
    ctrl = []
    for _c in range(nctrl):
        x = rd(10)
        y = rd(20)
        if x is None or y is None:
            break
        ctrl.append((x, y))
    while rd(42) is not None:                # 가중치 블록(있으면) 소비/무시
        pass
    nfit = rd(97)
    nfit = int(nfit) if nfit is not None else 0
    fit = []
    for _fp in range(nfit):
        x = rd(11)
        y = rd(21)
        if x is None or y is None:
            break
        fit.append((x, y))
    # 탄젠트(있으면 소비)
    rd(12); rd(22); rd(13); rd(23)

    if len(fit) >= 2:
        pts = fit                            # 피팅점은 곡선 위 → 좋은 근사
    elif len(ctrl) >= 2 and len(knots) >= len(ctrl) + degree + 1:
        pts = _bspline_points(ctrl, knots, degree, seg_len)
    else:
        pts = list(ctrl)
    return pts, i


def _hatch_edge_points(etype, edge, seg_len):
    """HATCH 엣지 경계의 한 엣지를 포인트 리스트로 변환한다.
    edge: 해당 엣지의 {group_code: [values]} 딕셔너리.
    etype: 1=LINE, 2=ARC, 3=ELLIPSE, 4=SPLINE.
    """
    def g(code, idx=0, d=0.0):
        try:
            return float(edge[code][idx])
        except (KeyError, IndexError, ValueError):
            return d

    if etype == 1:                                   # LINE
        return [(g(10), g(20)), (g(11), g(21))]
    if etype == 2:                                   # 원호 ARC
        ccw = int(g(73, 0, 1)) != 0
        return _arc_points(g(10), g(20), g(40), g(50), g(51), seg_len, ccw=ccw)
    if etype == 3:                                   # 타원호 ELLIPSE
        cx, cy = g(10), g(20)
        mx, my = g(11), g(21)                         # 장축 끝점(중심 기준 상대)
        ratio = g(40, 0, 1.0)                         # 단축/장축 비
        major = math.hypot(mx, my)
        if major < 1e-12:
            return []
        minor = major * ratio
        ang = math.atan2(my, mx)
        ca, sn = math.cos(ang), math.sin(ang)
        sa, ea = math.radians(g(50)), math.radians(g(51))
        ccw = int(g(73, 0, 1)) != 0
        if ccw:
            while ea <= sa:
                ea += 2 * math.pi
        else:
            while ea >= sa:
                ea -= 2 * math.pi
        sweep = ea - sa
        n = max(8, int(math.ceil((major + minor) / 2 * abs(sweep) / max(seg_len, 1e-6))))
        n = min(n, _MAX_ARC_POINTS)
        pts = []
        for k in range(n + 1):
            th = sa + sweep * (k / n)
            ex, ey = major * math.cos(th), minor * math.sin(th)
            pts.append((cx + ex * ca - ey * sn, cy + ex * sn + ey * ca))
        return pts
    if etype == 4:                                   # SPLINE (근사: 피팅점/제어점 연결)
        xs, ys = edge.get(11, []), edge.get(21, [])  # 피팅점 우선
        if not xs:
            xs, ys = edge.get(10, []), edge.get(20, [])  # 없으면 제어점
        pts = []
        for k in range(min(len(xs), len(ys))):
            try:
                pts.append((float(xs[k]), float(ys[k])))
            except ValueError:
                pass
        return pts
    return []


def _hatch_loops(ent, seg_len):
    """HATCH 엔티티의 경계 경로(들)를 폐루프 포인트 리스트로 추출한다.

    - 폴리라인 경계(92 & 2): 정점 + bulge(72 플래그) 지원.
    - 엣지 경계: LINE/ARC/ELLIPSE 지원, SPLINE 은 근사.
    씨드점/그라디언트 등 뒤따르는 데이터도 코드 10/20 을 쓰므로, 반드시 그룹코드
    등장 순서(_raw)를 따라가며 경계 블록만 정확히 읽는다. (OCS/extrusion 은 표준 2D 가정)
    """
    raw = ent.get("_raw", [])
    n = len(raw)
    i = 0
    while i < n and raw[i][0] != 91:                 # 91 = 경계 경로 수
        i += 1
    if i >= n:
        return []

    def fnum(v, d=0.0):
        try:
            return float(v)
        except ValueError:
            return d

    try:
        npaths = int(fnum(raw[i][1]))
    except ValueError:
        return []
    i += 1

    loops = []
    for _p in range(npaths):
        while i < n and raw[i][0] != 92:             # 92 = 경계 경로 타입 플래그
            i += 1
        if i >= n:
            break
        flag = int(fnum(raw[i][1]))
        i += 1

        if flag & 2:                                 # ---- 폴리라인 경계 ----
            has_bulge = 0
            closed = 0
            nverts = 0
            while i < n and raw[i][0] in (72, 73):
                if raw[i][0] == 72:
                    has_bulge = int(fnum(raw[i][1]))
                else:
                    closed = int(fnum(raw[i][1]))
                i += 1
            if i < n and raw[i][0] == 93:
                nverts = int(fnum(raw[i][1]))
                i += 1
            verts = []
            for _v in range(nverts):
                x = y = b = 0.0
                if i < n and raw[i][0] == 10:
                    x = fnum(raw[i][1]); i += 1
                if i < n and raw[i][0] == 20:
                    y = fnum(raw[i][1]); i += 1
                if has_bulge and i < n and raw[i][0] == 42:
                    b = fnum(raw[i][1]); i += 1
                verts.append((x, y, b))
            if verts:
                loops.append(_polyline_points(verts, bool(closed), seg_len))
        else:                                        # ---- 엣지 경계 ----
            nedges = 0
            if i < n and raw[i][0] == 93:
                nedges = int(fnum(raw[i][1]))
                i += 1
            pts = []
            for _e in range(nedges):
                while i < n and raw[i][0] != 72:
                    if raw[i][0] in (97, 92, 98):
                        break
                    i += 1
                if i >= n or raw[i][0] != 72:
                    break
                etype = int(fnum(raw[i][1]))
                i += 1
                if etype == 4:
                    # 스플라인 엣지는 개수(95/96/97)에 따라 구조대로 소비한다.
                    # (코드 97 이 스플라인 내부 '피팅점 수'와 경계 끝 '소스객체 수'로
                    #  중복되므로, 개수 기반으로 읽어야 피팅점 유실/이후 엣지 누락을 막는다.)
                    epts, i = _read_hatch_spline(raw, i, n, seg_len)
                else:
                    edge = {}
                    while i < n and raw[i][0] not in (72, 93, 97, 92, 98):
                        c, v = raw[i]
                        edge.setdefault(c, []).append(v)
                        i += 1
                    epts = _hatch_edge_points(etype, edge, seg_len)
                if epts:
                    if pts and math.hypot(pts[-1][0] - epts[0][0],
                                          pts[-1][1] - epts[0][1]) < 1e-9:
                        pts.extend(epts[1:])
                    else:
                        pts.extend(epts)
            if len(pts) >= 2:
                if not is_closed(pts):
                    pts.append(pts[0])
                loops.append(pts)
    return loops


def _entity_paths(ent, seg_len):
    t = ent["type"]

    if t == "LINE":
        return [[(_f(ent, 10), _f(ent, 20)), (_f(ent, 11), _f(ent, 21))]]

    if t == "HATCH":
        return _hatch_loops(ent, seg_len)

    if t == "POINT":
        return [[(_f(ent, 10), _f(ent, 20))]]

    if t == "CIRCLE":
        cx, cy, r = _f(ent, 10), _f(ent, 20), _f(ent, 40)
        return [_arc_points(cx, cy, r, 0, 360, seg_len, ccw=True)]

    if t == "ARC":
        cx, cy, r = _f(ent, 10), _f(ent, 20), _f(ent, 40)
        return [_arc_points(cx, cy, r, _f(ent, 50), _f(ent, 51), seg_len, ccw=True)]

    if t == "LWPOLYLINE":
        verts = _lwpoly_verts(ent)
        if not verts:
            return []
        closed = bool(int(_f(ent, 70)) & 1)
        return [_polyline_points(verts, closed, seg_len)]

    # POLYLINE 은 상위에서 VERTEX 와 함께 처리
    return None


def _old_polylines(entities, seg_len):
    """구형 POLYLINE + VERTEX + SEQEND 구조 처리 (VERTEX bulge 포함)."""
    paths = []
    i, n = 0, len(entities)
    while i < n:
        ent = entities[i]
        if ent["type"] == "POLYLINE":
            closed = bool(int(_f(ent, 70)) & 1)
            verts = []
            j = i + 1
            while j < n and entities[j]["type"] == "VERTEX":
                v = entities[j]
                verts.append((_f(v, 10), _f(v, 20), _f(v, 42)))  # x, y, bulge
                j += 1
            if j < n and entities[j]["type"] == "SEQEND":
                j += 1
            if verts:
                paths.append(_polyline_points(verts, closed, seg_len))
            i = j
        else:
            i += 1
    return paths


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------
def dxf_to_paths(path, seg_len=1.0):
    """
    DXF 파일을 경로 리스트로 변환한다.
    반환: [[(x,y), ...], [(x,y), ...], ...]  (경로별 포인트 리스트)
    unsupported 정보가 필요하면 dxf_to_paths_ex 사용.
    """
    paths, _ = dxf_to_paths_ex(path, seg_len)
    return paths


def dxf_to_paths_ex(path, seg_len=1.0):
    """dxf_to_paths + 미지원 엔티티 통계 반환: (paths, unsupported_dict)"""
    pairs = _read_pairs(path)
    entities = _parse_entities(pairs)

    paths = []
    paths.extend(_old_polylines(entities, seg_len))

    unsupported = {}
    for ent in entities:
        et = ent["type"]
        # POLYLINE 계열은 위에서 처리, 구조/테이블 토큰은 엔티티가 아니므로 제외
        if et in ("POLYLINE", "VERTEX", "SEQEND") or et in _STRUCTURAL:
            continue
        res = _entity_paths(ent, seg_len)
        if res is None:
            unsupported[et] = unsupported.get(et, 0) + 1
            continue
        paths.extend(res)
    return paths, unsupported


# ---------------------------------------------------------------------------
# 기하 유틸 / 슬라이스(면채우기) / 기준점 오프셋
# ---------------------------------------------------------------------------
def bounds(paths):
    """모든 경로의 경계 상자 (minx, miny, maxx, maxy). 비었으면 None."""
    xs, ys = [], []
    for pts in paths:
        for (x, y) in pts:
            xs.append(x)
            ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def is_closed(pts, tol=1e-6):
    """경로가 폐루프(면)인지 판정: 정점 3개 이상 & 시작=끝."""
    if len(pts) < 3:
        return False
    return math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) <= tol


def find_closed_loops(paths, tol=1e-6):
    """경로들 중 폐루프(면을 이루는 것)만 골라 반환한다."""
    return [pts for pts in paths if is_closed(pts, tol)]


def stitch_paths(paths, tol=1e-3):
    """끝점이 맞닿는(거리 ≤ tol) 열린 경로들을 이어 하나의 연속 경로로 결합한다.

    - 여러 개의 개별 LINE/열린 폴리라인이 실제로는 한 윤곽선을 이루는 경우
      (예: 사각형을 4개의 LINE 으로 그린 DXF) 이를 이어붙인다.
    - 체인이 시작점으로 되돌아오면 폐루프로 닫는다(마지막 점을 시작점에 정확히 스냅).
    - 이미 닫힌 경로 / 단일 점 경로는 그대로 통과.
    반환: 결합된 경로 리스트.
    """
    from collections import defaultdict

    def d(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1])

    def key(pt):
        # floor 사용: 두 점의 축 거리 ≤ tol 이면 셀 인덱스 차이 ≤ 1 이 보장되어
        # 3x3 이웃 검색으로 tol 이내의 모든 짝을 반드시 찾는다. (round 는 .5 경계에서
        # 셀 차이가 2까지 벌어져 누락될 수 있음)
        return (math.floor(pt[0] / tol), math.floor(pt[1] / tol))

    passthrough = []
    segs = []
    for p in paths:
        if len(p) < 2 or is_closed(p, tol):
            passthrough.append(list(p))
        else:
            segs.append(list(p))

    used = [False] * len(segs)
    endmap = defaultdict(list)   # 격자 셀 -> 끝점을 가진 세그먼트 인덱스들
    for i, s in enumerate(segs):
        endmap[key(s[0])].append(i)
        endmap[key(s[-1])].append(i)

    def candidates(pt):
        kx, ky = key(pt)
        seen = set()
        for dx in (-1, 0, 1):          # 격자 경계 문제 방지: 인접 9칸 검색
            for dy in (-1, 0, 1):
                for j in endmap.get((kx + dx, ky + dy), ()):
                    if j not in seen:
                        seen.add(j)
                        yield j

    result = list(passthrough)
    for i in range(len(segs)):
        if used[i]:
            continue
        chain = list(segs[i])
        used[i] = True
        for _ in range(2):             # 양방향(꼬리→, 뒤집어 다시 꼬리→) 확장
            # 연결되는 미사용 세그먼트가 없을 때까지 계속 확장한다.
            # (닫힘 여부로 중간에 끊지 않는다 — 시작점 근처를 지나쳐 이어지는
            #  열린 윤곽선이 잘려 남은 선분이 버려지는 문제를 방지)
            while True:
                tail = chain[-1]
                found = False
                for j in candidates(tail):
                    if used[j]:
                        continue
                    o = segs[j]
                    if d(tail, o[0]) <= tol:
                        chain.extend(o[1:])
                        used[j] = True
                        found = True
                        break
                    if d(tail, o[-1]) <= tol:
                        chain.extend(reversed(o[:-1]))
                        used[j] = True
                        found = True
                        break
                if not found:
                    break
            chain.reverse()
        # 폐루프면 시작점에 정확히 스냅해 확실히 닫는다
        if len(chain) >= 4 and d(chain[-1], chain[0]) <= tol:
            chain[-1] = chain[0]
        result.append(chain)
    return result


ANCHORS = ("none", "center", "bl", "tl", "br", "tr", "l", "r", "t", "b")


def anchor_point(paths, mode):
    """오브젝트 경계 기준 기준점(월드 좌표) 반환.
    mode: none(=DXF 원점 유지) / center(중앙) / 모서리 bl,tl,br,tr /
          변 중앙 l(좌),r(우),t(위),b(아래).
    """
    if mode == "none":
        return (0.0, 0.0)
    b = bounds(paths)
    if not b:
        return (0.0, 0.0)
    minx, miny, maxx, maxy = b
    cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    return {
        "center": (cx, cy),
        "bl": (minx, miny), "tl": (minx, maxy),
        "br": (maxx, miny), "tr": (maxx, maxy),
        "l": (minx, cy), "r": (maxx, cy),
        "t": (cx, maxy), "b": (cx, miny),
    }.get(mode, (0.0, 0.0))


def offset_paths(paths, origin):
    """origin(월드 좌표)이 (0,0)이 되도록 모든 경로를 평행이동한 새 리스트 반환."""
    ox, oy = origin
    if ox == 0.0 and oy == 0.0:
        return [list(pts) for pts in paths]
    return [[(x - ox, y - oy) for (x, y) in pts] for pts in paths]


def generate_infill(loops, spacing, angle_deg=0.0, zigzag=False):
    """폐루프(면)를 일정 간격/방향의 스캔라인으로 채우는 경로를 생성한다.

    loops    : 폐루프 포인트 리스트들 [[(x,y),...], ...]
    spacing  : 슬라이스 간격(mm)
    angle_deg: 채우기 선의 방향(도). 0=가로선, 90=세로선, 45=대각선 등.
    zigzag   : True 면 한 붓 그리기(연속 경로 1개), False 면 개별 선분 경로들.

    반환: 경로 리스트 (각 경로는 [(x,y),...]). 월드 좌표계로 되돌려 반환.
    even-odd 규칙으로 여러 루프/구멍을 함께 처리한다.
    """
    if not loops or spacing <= 0 or not math.isfinite(spacing):
        return []

    th = math.radians(angle_deg)
    # 채우기 선을 수평으로 만들기 위해 -th 회전, 결과는 +th 로 복원
    fc, fs = math.cos(-th), math.sin(-th)
    ic, is_ = math.cos(th), math.sin(th)

    def fwd(p):
        x, y = p
        return (x * fc - y * fs, x * fs + y * fc)

    def inv(x, y):
        return (x * ic - y * is_, x * is_ + y * ic)

    rloops = [[fwd(p) for p in loop] for loop in loops]
    ys = [y for loop in rloops for (_x, y) in loop]
    miny, maxy = min(ys), max(ys)

    # 비어있지 않은 스캔라인만 순서대로 모은다: [(y, [(xa,xb),... xa<xb]) ...]
    k0 = math.ceil(miny / spacing)
    y = k0 * spacing
    scanlines = []
    count = 0
    while y <= maxy + 1e-9 and count < _MAX_INFILL_LINES:
        count += 1
        xs = []
        for loop in rloops:
            n = len(loop)
            for i in range(n - 1):
                x1, y1 = loop[i]
                x2, y2 = loop[i + 1]
                if y1 == y2:              # 수평 에지는 스킵
                    continue
                lo, hi = (y1, y2) if y1 < y2 else (y2, y1)
                if lo <= y < hi:          # half-open: 정점 중복 카운트 방지
                    t = (y - y1) / (y2 - y1)
                    xs.append(x1 + t * (x2 - x1))
        xs.sort()
        segs = []
        for j in range(0, len(xs) - 1, 2):
            a, b = xs[j], xs[j + 1]
            if b - a > 1e-9:              # 영길이(극점 중복) 세그먼트 제거
                segs.append((a, b))
        if segs:
            scanlines.append((y, segs))
        y += spacing

    if not scanlines:
        return []

    if not zigzag:
        # 개별 선분: 방향 일정 (왼->오)
        return [[inv(a, y), inv(b, y)] for (y, segs) in scanlines for (a, b) in segs]

    # 지그재그: 인접(연속 스캔라인 & x구간 겹침)한 세그먼트만 하나의 스트로크로 잇고,
    # 그렇지 않으면(구멍/빈틈/분리도형) 새 스트로크로 분리해 빈 공간을 가로지르지 않는다.
    strokes = []
    cur = None
    prev_y = None
    prev_int = None
    for li, (y, segs) in enumerate(scanlines):
        oriented = list(segs)                        # 왼->오
        if li % 2 == 1:                              # 홀수(비어있지 않은) 줄 방향 반전
            oriented = [(b, a) for (a, b) in reversed(segs)]
        for (s, e) in oriented:
            lo, hi = (s, e) if s < e else (e, s)
            adjacent = (
                cur is not None and prev_y is not None
                and 0 < (y - prev_y) <= spacing * 1.5          # 연속 스캔라인
                and max(prev_int[0], lo) <= min(prev_int[1], hi) + 1e-9  # x 겹침
            )
            if adjacent:
                cur.append((s, y))
                cur.append((e, y))
            else:
                if cur:
                    strokes.append(cur)
                cur = [(s, y), (e, y)]
            prev_y = y
            prev_int = (lo, hi)
    if cur:
        strokes.append(cur)

    return [[inv(px, py) for (px, py) in stroke] for stroke in strokes]


def order_paths(items, start=(0.0, 0.0), close_tol=1e-6, max_items=4000):
    """경로들을 펜업(점프) 이동이 최소가 되도록 재정렬/방향조정한다 (그리디 최근접).

    items : [(points, tag), ...]  — tag(예: 'outline'/'infill')는 그대로 따라간다.
    start : 시작 펜 위치(월드 좌표).
    반환  : 재정렬된 [(points, tag), ...].
            - 열린 경로: 현재 펜에 가까운 끝이 시작이 되도록 필요시 역방향.
            - 닫힌 경로: 현재 펜에 가장 가까운 정점에서 시작하도록 회전(끝=시작 유지).
    항목 수가 max_items 초과면 성능 보호를 위해 원본 순서를 그대로 반환한다.
    """
    n = len(items)
    if n <= 1 or n > max_items:
        return list(items)

    def d(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1])

    prepared = [(pts, tag, is_closed(pts, close_tol) and len(pts) >= 4)
                for pts, tag in items]

    used = [False] * n
    cur = start
    result = []
    for _ in range(n):
        best_j = -1
        best_cost = None
        best_seq = None
        for j in range(n):
            if used[j]:
                continue
            pts, tag, closed = prepared[j]
            if closed:
                uniq = pts[:-1]                    # 마지막(=처음) 중복 제거
                ci = min(range(len(uniq)), key=lambda k: d(uniq[k], cur))
                cost = d(uniq[ci], cur)
                seq = uniq[ci:] + uniq[:ci] + [uniq[ci]]   # 회전 후 닫기
            else:
                d0 = d(pts[0], cur)
                d1 = d(pts[-1], cur)
                if d1 < d0:
                    cost, seq = d1, list(reversed(pts))
                else:
                    cost, seq = d0, list(pts)
            if best_cost is None or cost < best_cost:
                best_cost, best_j, best_seq = cost, j, seq
        used[best_j] = True
        result.append((best_seq, prepared[best_j][1]))
        cur = best_seq[-1]
    return result


def paths_to_point_text(paths, precision=3, blank_between=True):
    """
    경로 리스트를 X Y 포인트 텍스트로 변환 (G 명령어 없음).
    경로 사이에는 빈 줄(펜 업/이동 구분)을 넣는다.
    """
    lines = []
    for pi, pts in enumerate(paths):
        if blank_between and pi > 0:
            lines.append("")  # 경로 구분 (펜 업)
        for (x, y) in pts:
            lines.append(f"{x:.{precision}f} {y:.{precision}f}")
    return "\n".join(lines) + "\n"


def paths_to_gcode_text(paths, precision=3, blank_between=True):
    """경로 리스트를 G-code 형식 텍스트로 변환한다.

    각 좌표 한 줄:  G1 X<x> Y<y>   (X, Y 앞에 공백 1칸)
    경로 사이에는 빈 줄(펜 업/이동 구분)을 넣는다. 소수점은 precision 자리.
    """
    lines = []
    for pi, pts in enumerate(paths):
        if blank_between and pi > 0:
            lines.append("")  # 경로 구분 (펜 업)
        for (x, y) in pts:
            lines.append(f"G1 X{x:.{precision}f} Y{y:.{precision}f}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("사용법: python dxf_points.py <input.dxf> [seg_len]")
        sys.exit(1)
    seg = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
    ps, unsup = dxf_to_paths_ex(sys.argv[1], seg)
    print(paths_to_point_text(ps))
    total = sum(len(p) for p in ps)
    print(f"# 경로 {len(ps)}개, 포인트 {total}개", file=sys.stderr)
    if unsup:
        print("# 미지원:", unsup, file=sys.stderr)
