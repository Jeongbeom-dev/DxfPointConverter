# -*- coding: utf-8 -*-
"""
gui.py
------
DXF -> 포인트(X, Y) 변환 GUI 프로그램

기능:
  1) DXF 파일 입력 (파일 열기)
  2) 포인트(X Y) 출력 - G 명령어 없이 좌표만
  3) 1-layer 방식으로 화면상 포인트 경로 시각화 (Canvas)
     - 마우스 휠 줌(커서 기준) / 드래그 팬 / 화면맞춤
     - 적응형 격자 + X/Y 축 + 원점 표시
     - 펜업(이동) 경로 점선 표시
     - 경로 방향 화살표
     - 그리기 순서 애니메이션
     - 마우스 위치 실시간 좌표 표시
  4) 포인트 텍스트 저장

실행:  python gui.py
표준 라이브러리 tkinter 만 사용 (별도 설치 불필요)
"""

import math
import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from dxf_points import (
    dxf_to_paths_ex, paths_to_gcode_text,
    find_closed_loops, generate_infill, anchor_point, offset_paths, bounds,
    stitch_paths, order_paths,
)


# 배율(scale) 허용 범위 — 0/음수/언더플로/오버플로로 인한 렌더 폭주·크래시 방지
APP_VERSION = "1.1.0"      # 프로그램 버전 (git 태그와 일치)

SCALE_MIN = 1e-6
SCALE_MAX = 1e7

# 색상 테마
BG = "#111418"
GRID = "#1c2128"
GRID_MAJOR = "#2a323c"
AXIS = "#3a4654"
TRAVEL = "#5a6472"       # 펜업(이동) 점선 색
INFILL = "#7d8ea8"       # 면채우기(슬라이스) 선 색
CURSOR = "#ffd54f"       # 재생 중 현재 지점 강조 마커 색
ORIGIN = "#e06c75"
START_MARK = "#ffffff"
PALETTE = ["#4fc3f7", "#81c784", "#ffb74d", "#e57373",
           "#ba68c8", "#4db6ac", "#fff176", "#f06292",
           "#9ccc65", "#7986cb"]


def nice_step(raw):
    """raw 이상이면서 1/2/5 x 10^k 형태인 '보기 좋은' 격자 간격 반환."""
    import math
    if raw <= 0:
        return 1.0
    exp = math.floor(math.log10(raw))
    base = 10 ** exp
    for m in (1, 2, 5, 10):
        if m * base >= raw:
            return m * base
    return 10 * base


class DxfPointApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"DXF → G-code 변환기  v{APP_VERSION}")
        self.root.geometry("1360x780")
        self.root.minsize(1160, 640)

        # 데이터
        self.raw_paths = []        # DXF 원본 외곽선 (기준점 이동 전, 불변)
        self.infill_paths = []     # 면채우기(슬라이스) 생성 경로 (원본 좌표계)
        self.paths = []            # 표시/출력용 = (외곽선+채우기) 에 기준점 오프셋 적용
        self.path_kinds = []       # self.paths 와 1:1: 'outline' | 'infill'
        self.origin = (0.0, 0.0)   # (0,0)이 될 월드 좌표 (기준점)
        self.origin_mode = "none"
        self.current_file = None
        self._body = ""            # 생성된 G-code 본문 (헤더 제외)
        self._outlines_cache = None  # stitch 결과 캐시 (raw_paths/stitch 변경 시 무효화)

        # 화면 변환 상태 (world=DXF 좌표 -> screen 픽셀)
        #   sx = ox + wx * scale
        #   sy = oy - wy * scale   (Y 뒤집기)
        self.scale = 1.0
        self.ox = 0.0
        self.oy = 0.0
        self._need_fit = True

        # 표시 옵션
        self.show_points = tk.BooleanVar(value=True)
        self.show_lines = tk.BooleanVar(value=True)
        self.show_travel = tk.BooleanVar(value=True)
        self.show_dir = tk.BooleanVar(value=False)
        self.show_grid = tk.BooleanVar(value=True)
        self.show_start = tk.BooleanVar(value=True)
        self.show_infill = tk.BooleanVar(value=True)

        # 면채우기(슬라이스) 옵션
        self.fill_spacing = tk.StringVar(value="1.0")
        self.fill_angle = tk.StringVar(value="0")
        self.fill_zigzag = tk.BooleanVar(value=True)
        self.stitch_on = tk.BooleanVar(value=False)   # 선분 결합(끝점 이어 폐루프화)
        self.optimize_order = tk.BooleanVar(value=True)  # 한붓 최적화(점프 최소화)

        # 애니메이션 상태
        self._anim_job = None
        self._anim_reveal = 0      # 표시할 세그먼트 수
        self._anim_accum = 0.0     # 소수 속도 누적(1 이하 속도 지원)
        self._animating = False    # 재생 중 여부 (강조 마커 표시용)
        self._segments = []        # [(kind, x1,y1,x2,y2)] kind: 'cut'|'travel'
        self._verts = []           # [(x,y,is_start,path_idx)]  순서대로
        self.anim_speed = tk.DoubleVar(value=5.0)      # 프레임당 세그먼트 수(0.1~15)
        self.anim_speed_txt = tk.StringVar(value="5.0")

        self._pan_start = None
        self._redraw_job = None    # 상호작용 중 redraw 코얼레싱용 after_idle 핸들

        self._build_ui()
        self._bind_canvas()
        # 창 닫힐 때 예약된 작업 정리
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        # ---- 상단 툴바 (2줄) ----
        bar1 = ttk.Frame(self.root, padding=(6, 6, 6, 2))
        bar1.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(bar1, text="📂 DXF 열기", command=self.open_dxf).pack(side=tk.LEFT)

        ttk.Label(bar1, text="  분할 간격:").pack(side=tk.LEFT)
        self.seg_var = tk.StringVar(value="1.0")
        seg_entry = ttk.Entry(bar1, textvariable=self.seg_var, width=6)
        seg_entry.pack(side=tk.LEFT)
        seg_entry.bind("<Return>", lambda e: self.reconvert())
        ttk.Button(bar1, text="재변환", command=self.reconvert).pack(side=tk.LEFT, padx=4)

        ttk.Separator(bar1, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        ttk.Button(bar1, text="화면맞춤", command=self.fit_view).pack(side=tk.LEFT)
        ttk.Button(bar1, text="＋", width=3, command=lambda: self.zoom_center(1.25)).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar1, text="－", width=3, command=lambda: self.zoom_center(0.8)).pack(side=tk.LEFT)

        ttk.Separator(bar1, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        ttk.Button(bar1, text="▶ 재생", command=self.anim_play).pack(side=tk.LEFT)
        ttk.Button(bar1, text="■ 정지", command=self.anim_stop).pack(side=tk.LEFT, padx=2)
        ttk.Label(bar1, text=" 속도:").pack(side=tk.LEFT)
        ttk.Scale(bar1, from_=0.1, to=15, orient=tk.HORIZONTAL, length=90,
                  variable=self.anim_speed,
                  command=lambda v: self.anim_speed_txt.set(f"{float(v):.1f}")).pack(side=tk.LEFT)
        ttk.Label(bar1, textvariable=self.anim_speed_txt, width=4,
                  anchor=tk.W).pack(side=tk.LEFT)

        ttk.Button(bar1, text="💾 저장(.gcode)", command=self.save_points).pack(side=tk.RIGHT)

        # ---- 표시 토글 (2번째 줄) ----
        bar2 = ttk.Frame(self.root, padding=(6, 0, 6, 4))
        bar2.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(bar2, text="표시:").pack(side=tk.LEFT)
        for text, var in (("점", self.show_points), ("경로선", self.show_lines),
                          ("이동선(펜업)", self.show_travel), ("방향", self.show_dir),
                          ("격자", self.show_grid), ("시작점", self.show_start),
                          ("채우기", self.show_infill)):
            ttk.Checkbutton(bar2, text=text, variable=var,
                            command=self.redraw).pack(side=tk.LEFT, padx=2)

        # ---- 기준점 / 면채우기 (3번째 줄) ----
        bar3 = ttk.Frame(self.root, padding=(6, 0, 6, 4))
        bar3.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(bar3, text="기준점:").pack(side=tk.LEFT)
        for label, mode in (("원본", "none"), ("중심", "center"), ("좌하", "bl"),
                            ("좌상", "tl"), ("우하", "br"), ("우상", "tr")):
            ttk.Button(bar3, text=label, width=4,
                       command=lambda m=mode: self.set_origin(m)).pack(side=tk.LEFT, padx=1)

        ttk.Separator(bar3, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        ttk.Checkbutton(bar3, text="선분 결합(폐루프화)", variable=self.stitch_on,
                        command=self._on_stitch_toggle).pack(side=tk.LEFT)
        ttk.Checkbutton(bar3, text="한붓 최적화", variable=self.optimize_order,
                        command=self._rebuild).pack(side=tk.LEFT, padx=(6, 0))

        ttk.Separator(bar3, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        ttk.Label(bar3, text="면채우기  간격:").pack(side=tk.LEFT)
        ttk.Entry(bar3, textvariable=self.fill_spacing, width=5).pack(side=tk.LEFT)
        ttk.Label(bar3, text=" 방향:").pack(side=tk.LEFT)
        angle_cb = ttk.Combobox(bar3, textvariable=self.fill_angle, width=6,
                                values=("0", "45", "90", "135"))
        angle_cb.pack(side=tk.LEFT)
        ttk.Label(bar3, text="°").pack(side=tk.LEFT)
        ttk.Checkbutton(bar3, text="지그재그", variable=self.fill_zigzag).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar3, text="채우기 생성", command=self.generate_fill).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar3, text="채우기 지움", command=self.clear_fill).pack(side=tk.LEFT)

        # ---- 본문 ----
        body = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 4))

        left = ttk.Frame(body)
        self.canvas = tk.Canvas(left, bg=BG, highlightthickness=1,
                                highlightbackground="#333")
        self.canvas.pack(fill=tk.BOTH, expand=True)
        body.add(left, weight=3)

        right = ttk.Frame(body)
        # 헤더 입력 (출력 파일 상단에 삽입됨)
        ttk.Label(right, text="헤더 (출력 상단에 삽입)").pack(anchor=tk.W)
        self.header = tk.Text(right, width=34, height=4, wrap=tk.NONE,
                              font=("Consolas", 10), bg="#12181f", fg="#9fd0a0",
                              insertbackground="#c9d1d9")
        self.header.pack(fill=tk.X, pady=(0, 4))
        self.header.insert("1.0", "; DXF to G-code\n")
        self.header.bind("<KeyRelease>", lambda e: self._refresh_output())

        ttk.Label(right, text="출력 미리보기 (G01  X  Y)").pack(anchor=tk.W)
        txt_frame = ttk.Frame(right)
        txt_frame.pack(fill=tk.BOTH, expand=True)
        self.text = tk.Text(txt_frame, width=34, wrap=tk.NONE,
                            font=("Consolas", 10), bg="#0d1117", fg="#c9d1d9",
                            insertbackground="#c9d1d9")
        yscroll = ttk.Scrollbar(txt_frame, orient=tk.VERTICAL, command=self.text.yview)
        self.text.configure(yscrollcommand=yscroll.set)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        body.add(right, weight=2)

        # ---- 상태바 ----
        status_frame = ttk.Frame(self.root)
        status_frame.pack(side=tk.BOTTOM, fill=tk.X)
        self.status = tk.StringVar(value="DXF 파일을 열어주세요.  (휠=줌, 드래그=이동, 더블클릭=화면맞춤)")
        ttk.Label(status_frame, textvariable=self.status, relief=tk.SUNKEN,
                  anchor=tk.W, padding=4).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.coord = tk.StringVar(value="X: -  Y: -")
        ttk.Label(status_frame, textvariable=self.coord, relief=tk.SUNKEN,
                  anchor=tk.E, padding=4, width=24).pack(side=tk.RIGHT)

    def _bind_canvas(self):
        c = self.canvas
        c.bind("<Configure>", self._on_resize)
        c.bind("<MouseWheel>", self._on_wheel)             # Windows / macOS
        c.bind("<Button-4>", lambda e: self._on_wheel(e, 120))   # Linux up
        c.bind("<Button-5>", lambda e: self._on_wheel(e, -120))  # Linux down
        c.bind("<ButtonPress-1>", self._on_pan_start)
        c.bind("<B1-Motion>", self._on_pan_move)
        c.bind("<ButtonRelease-1>", self._on_pan_end)
        c.bind("<Double-Button-1>", lambda e: self.fit_view())
        c.bind("<Motion>", self._on_motion)
        c.bind("<Leave>", lambda e: self.coord.set("X: -  Y: -"))

    # ---------------------------------------------------------- 좌표 변환
    def w2s(self, x, y):
        return self.ox + x * self.scale, self.oy - y * self.scale

    def s2w(self, sx, sy):
        return (sx - self.ox) / self.scale, (self.oy - sy) / self.scale

    def _bounds(self):
        xs, ys = [], []
        for pts in self.paths:
            for (x, y) in pts:
                xs.append(x)
                ys.append(y)
        if not xs:
            return None
        return min(xs), min(ys), max(xs), max(ys)

    # ------------------------------------------------------------- 파일
    def open_dxf(self):
        fpath = filedialog.askopenfilename(
            title="DXF 파일 선택",
            filetypes=[("DXF 파일", "*.dxf"), ("모든 파일", "*.*")])
        if not fpath:
            return
        self.current_file = fpath
        self._need_fit = True
        # 새 파일 로드에서만 기준점/채우기 초기화 (재변환 시에는 보존)
        self.origin_mode = "none"
        self.infill_paths = []
        self.convert()

    def _get_seg(self):
        try:
            seg = float(self.seg_var.get())
            if not math.isfinite(seg) or seg <= 0:   # nan / inf / 0 이하 거부
                raise ValueError
            return seg
        except ValueError:
            messagebox.showwarning("입력 오류", "분할 간격은 0보다 큰 유한한 숫자여야 합니다.")
            self.seg_var.set("1.0")
            return 1.0

    def reconvert(self):
        if self.current_file:
            self.convert()

    def convert(self):
        self.anim_stop()
        seg = self._get_seg()
        try:
            raw, self._unsupported = dxf_to_paths_ex(self.current_file, seg)
        except Exception as e:
            messagebox.showerror("변환 오류", str(e))
            return
        had_fill = bool(self.infill_paths)
        self.raw_paths = raw
        self._outlines_cache = None        # raw_paths 변경 -> 결합 캐시 무효화
        # 재변환으로 세그먼트가 바뀌면 기존 채우기를 현재 설정으로 재생성 (기준점은 보존)
        if had_fill:
            self._regen_fill_silent()
        self._rebuild()

    def _outlines(self):
        """표시/채우기용 외곽선. '선분 결합'이 켜져 있으면 끝점을 이어 폐루프화한다.
        stitch 결과는 캐시한다 (raw_paths/stitch 변경 시 무효화)."""
        if self._outlines_cache is None:
            self._outlines_cache = (stitch_paths(self.raw_paths)
                                    if self.stitch_on.get() else self.raw_paths)
        return self._outlines_cache

    def _on_stitch_toggle(self):
        if not self.raw_paths:
            return
        self._outlines_cache = None       # 결합 상태 변경 -> 캐시 무효화
        if self.infill_paths:
            self._regen_fill_silent()
        self._rebuild()

    def _fill_est_lines(self, loops, spacing):
        """채우기 스캔라인 예상 개수 (과도 촘촘 판정용). 경계 없으면 0."""
        b = bounds(loops)
        if not b or spacing <= 0:
            return 0
        return math.hypot(b[2] - b[0], b[3] - b[1]) / spacing

    def _regen_fill_silent(self):
        """현재 채우기 설정으로 infill 을 조용히(팝업 없이) 재생성한다.
        입력이 무효하거나 너무 촘촘하면 기존 채우기를 그대로 둔다(소실 방지)."""
        try:
            spacing = float(self.fill_spacing.get())
            angle = float(self.fill_angle.get())
            if not (math.isfinite(spacing) and spacing > 0 and math.isfinite(angle)):
                raise ValueError
        except ValueError:
            return                         # 무효 입력 -> 기존 채우기 유지
        loops = find_closed_loops(self._outlines())
        if not loops:
            self.infill_paths = []         # 채울 면이 없음
            return
        if self._fill_est_lines(loops, spacing) > 50000:
            return                         # 너무 촘촘 -> 조용히 스킵(기존 유지)
        self.infill_paths = generate_infill(
            loops, spacing, angle, zigzag=self.fill_zigzag.get())

    def _rebuild(self, refit=False):
        """외곽선+채우기를 합치고 기준점 오프셋을 적용해 표시/출력을 재구성한다."""
        outlines = self._outlines()
        # 기준점(월드 좌표) = 오브젝트 경계 기준 앵커 (채우기 제외)
        self.origin = anchor_point(outlines, self.origin_mode)

        # 외곽선 + 채우기를 하나의 목록으로 (태그 유지)
        items = [(pts, "outline") for pts in outlines]
        items += [(pts, "infill") for pts in self.infill_paths]
        # 한붓 최적화: 펜업(점프) 최소가 되도록 재정렬/방향조정
        if self.optimize_order.get() and len(items) > 1:
            items = order_paths(items, start=self.origin)

        combined = [pts for pts, _tag in items]
        kinds = [tag for _pts, tag in items]

        self.paths = offset_paths(combined, self.origin)
        self.path_kinds = kinds

        # G-code 본문(G01 X Y, 3자리) 생성 후 헤더와 합쳐 미리보기 갱신
        self._body = paths_to_gcode_text(self.paths, precision=3, blank_between=True)
        self._refresh_output()

        self._build_segments()
        self._anim_reveal = len(self._segments)

        total = sum(len(p) for p in self.paths)
        name = os.path.basename(self.current_file) if self.current_file else "-"
        n_out = len(outlines)
        n_fill = len(self.infill_paths)
        n_loops = len(find_closed_loops(outlines))
        origin_label = {"none": "원본", "center": "중심", "bl": "좌하",
                        "tl": "좌상", "br": "우하", "tr": "우상"}.get(self.origin_mode, "원본")
        stitch_tag = "  [결합]" if self.stitch_on.get() else ""
        msg = (f"{name}{stitch_tag}  |  외곽선 {n_out}(폐루프 {n_loops})  채우기 {n_fill}"
               f"  |  포인트 {total}"
               f"  |  기준점 {origin_label}({self.origin[0]:.2f},{self.origin[1]:.2f})")
        if getattr(self, "_unsupported", None):
            msg += "  |  미지원: " + ", ".join(f"{k}×{v}" for k, v in self._unsupported.items())
        self.status.set(msg)

        if self._need_fit:
            self.fit_view()
            self._need_fit = False
        elif refit:
            self.fit_view()
        else:
            self.redraw()

    # ------------------------------------------------- 기준점 / 면채우기
    def set_origin(self, mode):
        if not self.raw_paths:
            messagebox.showinfo("알림", "먼저 DXF 파일을 열어주세요.")
            return
        self.origin_mode = mode
        self._rebuild(refit=True)

    def generate_fill(self):
        if not self.raw_paths:
            messagebox.showinfo("알림", "먼저 DXF 파일을 열어주세요.")
            return
        # 간격
        try:
            spacing = float(self.fill_spacing.get())
            if not math.isfinite(spacing) or spacing <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("입력 오류", "채우기 간격은 0보다 큰 유한한 숫자여야 합니다.")
            self.fill_spacing.set("1.0")
            return
        # 방향(각도)
        try:
            angle = float(self.fill_angle.get())
            if not math.isfinite(angle):
                raise ValueError
        except ValueError:
            messagebox.showwarning("입력 오류", "방향(각도)은 숫자여야 합니다.")
            self.fill_angle.set("0")
            return

        loops = find_closed_loops(self._outlines())
        if not loops:
            # 결합을 켜면 폐루프가 생기는 경우 안내
            if not self.stitch_on.get() and find_closed_loops(stitch_paths(self.raw_paths)):
                messagebox.showinfo(
                    "알림",
                    "닫힌 면이 없습니다.\n선분이 따로 그려진 도형입니다 — "
                    "'선분 결합(폐루프화)'을 켜면 이어붙여 채울 수 있습니다.")
            else:
                messagebox.showinfo("알림", "면(폐루프)이 없습니다. 채우기는 닫힌 도형에만 적용됩니다.")
            return
        # 과도하게 촘촘한 간격 사전 검증 (침묵 절단 방지)
        est_lines = self._fill_est_lines(loops, spacing)
        if est_lines > 50000:
            messagebox.showwarning(
                "입력 오류",
                f"간격({spacing})이 도형 크기에 비해 너무 촘촘합니다 "
                f"(약 {int(est_lines):,}줄). 간격을 크게 하세요.")
            return
        self.infill_paths = generate_infill(loops, spacing, angle,
                                            zigzag=self.fill_zigzag.get())
        if not self.infill_paths:
            messagebox.showinfo("알림", "생성된 채우기 경로가 없습니다. 간격을 줄여보세요.")
        self._rebuild()

    def clear_fill(self):
        if self.infill_paths:
            self.infill_paths = []
            self._rebuild()

    def _build_segments(self):
        """애니메이션/그리기용 순서 세그먼트 & 정점 리스트 구성.
        세그먼트: (kind, x1, y1, x2, y2, path_idx)   kind: 'cut' | 'travel'
        """
        self._segments = []
        self._verts = []
        prev_end = None
        for pi, pts in enumerate(self.paths):
            if not pts:
                continue
            # 펜업 이동 (이전 경로 끝 -> 현재 경로 시작)
            if prev_end is not None:
                self._segments.append(("travel", prev_end[0], prev_end[1],
                                       pts[0][0], pts[0][1], pi))
            self._verts.append((pts[0][0], pts[0][1], True, pi))
            # 절삭 세그먼트
            for i in range(1, len(pts)):
                self._segments.append(("cut", pts[i - 1][0], pts[i - 1][1],
                                       pts[i][0], pts[i][1], pi))
                self._verts.append((pts[i][0], pts[i][1], False, pi))
            prev_end = pts[-1]

    # -------------------------------------------------------- 줌 / 팬 / 맞춤
    def _clamp_scale(self):
        """배율을 허용 범위로 제한 (0/음수/언더플로/오버플로 방지)."""
        if not math.isfinite(self.scale):
            self.scale = 1.0
        self.scale = max(SCALE_MIN, min(SCALE_MAX, self.scale))

    def fit_view(self):
        b = self._bounds()
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if not b or cw < 10 or ch < 10:
            return
        minx, miny, maxx, maxy = b
        margin = 40
        # 여백을 뺀 가용 영역은 항상 양수가 되도록 하한을 둔다 (얇은 슬라이버 대비)
        avail_w = max(cw - 2 * margin, 20)
        avail_h = max(ch - 2 * margin, 20)
        w = max(maxx - minx, 1e-6)
        h = max(maxy - miny, 1e-6)
        self.scale = min(avail_w / w, avail_h / h)
        self._clamp_scale()
        # 도형 중심을 캔버스 중심에 맞춤
        cx = (minx + maxx) / 2
        cy = (miny + maxy) / 2
        self.ox = cw / 2 - cx * self.scale
        self.oy = ch / 2 + cy * self.scale
        self.redraw()

    def zoom_center(self, factor):
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        self._zoom_at(cw / 2, ch / 2, factor)

    def _zoom_at(self, sx, sy, factor):
        wx, wy = self.s2w(sx, sy)          # s2w 는 항상 양의 scale 사용 (0나눗셈 없음)
        self.scale *= factor
        self._clamp_scale()
        # 커서 아래 world 점이 그대로 유지되도록 offset 조정
        self.ox = sx - wx * self.scale
        self.oy = sy + wy * self.scale
        self._schedule_redraw()

    def _on_wheel(self, event, delta=None):
        d = delta if delta is not None else event.delta
        factor = 1.15 ** (d / 120.0)
        self._zoom_at(event.x, event.y, factor)

    def _on_pan_start(self, event):
        self._pan_start = (event.x, event.y, self.ox, self.oy)

    def _on_pan_move(self, event):
        if not self._pan_start:
            return
        sx0, sy0, ox0, oy0 = self._pan_start
        self.ox = ox0 + (event.x - sx0)
        self.oy = oy0 + (event.y - sy0)
        self._schedule_redraw()

    def _on_pan_end(self, event):
        self._pan_start = None

    def _schedule_redraw(self):
        """상호작용(팬/줌) 중 다시그리기를 after_idle 로 합쳐 렉을 줄인다."""
        if self._redraw_job is None:
            self._redraw_job = self.root.after_idle(self._do_redraw)

    def _do_redraw(self):
        self._redraw_job = None
        self.redraw()

    def _on_resize(self, event):
        if self._need_fit and self.paths:
            self.fit_view()
            self._need_fit = False
        else:
            self.redraw()

    def _on_motion(self, event):
        wx, wy = self.s2w(event.x, event.y)
        self.coord.set(f"X: {wx:.3f}  Y: {wy:.3f}")

    # ------------------------------------------------------------- 애니메이션
    def _anim_limit(self):
        """애니메이션이 도달할 세그먼트 상한. 채우기 숨김 시 마지막 외곽선까지만
        (숨긴 채우기 구간에서 화면이 멈춘 듯 보이는 데드타임 방지)."""
        if self.show_infill.get():
            return len(self._segments)
        limit = 0
        for idx, seg in enumerate(self._segments):
            if self._kind_of(seg[5]) != "infill":
                limit = idx + 1
        return limit

    def anim_play(self):
        if not self._segments:
            return
        self.anim_stop()
        self._anim_reveal = 0
        self._anim_accum = 0.0
        self._animating = True
        self._anim_step()

    def anim_stop(self):
        if self._anim_job is not None:
            self.root.after_cancel(self._anim_job)
            self._anim_job = None
        self._animating = False
        # 정지 시 전체 표시
        self._anim_reveal = len(self._segments)
        self.redraw()

    def _on_close(self):
        """창 종료: 예약된 애니메이션/다시그리기 콜백을 취소한 뒤 파괴."""
        if self._anim_job is not None:
            self.root.after_cancel(self._anim_job)
            self._anim_job = None
        if self._redraw_job is not None:
            self.root.after_cancel(self._redraw_job)
            self._redraw_job = None
        self.root.destroy()

    def _anim_step(self):
        limit = self._anim_limit()
        speed = max(0.1, float(self.anim_speed.get()))   # 0.1~15, 1 이하 지원
        self._anim_accum += speed
        add = int(self._anim_accum)                       # 소수 누적: <1 이면 여러 프레임에 1칸
        if add:
            self._anim_accum -= add
            self._anim_reveal = min(limit, self._anim_reveal + add)
        self.redraw()
        if self._anim_reveal < limit:
            self._anim_job = self.root.after(30, self._anim_step)
        else:
            self._anim_job = None
            self._animating = False
            self.redraw()                                # 마지막 강조 마커 정리

    # ------------------------------------------------------------- 그리기
    def redraw(self):
        c = self.canvas
        c.delete("all")
        cw = c.winfo_width()
        ch = c.winfo_height()
        if cw < 10 or ch < 10:
            return

        if self.show_grid.get():
            self._draw_grid(cw, ch)

        if not self.paths:
            c.create_text(cw / 2, ch / 2, fill="#55606b", font=("맑은 고딕", 12),
                          text="DXF 파일을 열어주세요")
            return

        reveal = self._anim_reveal
        arrows_ok = self.show_dir.get() and len(self._segments) <= 800
        show_lines = self.show_lines.get()
        show_travel = self.show_travel.get()
        show_infill = self.show_infill.get()

        # 세그먼트 그리기 (순서대로, reveal 까지)
        for drawn, (kind, x1, y1, x2, y2, pi) in enumerate(self._segments):
            if drawn >= reveal:
                break
            is_infill = self._kind_of(pi) == "infill"
            if is_infill and not show_infill:
                continue
            sx1, sy1 = self.w2s(x1, y1)
            sx2, sy2 = self.w2s(x2, y2)
            if kind == "travel":
                if show_travel:
                    c.create_line(sx1, sy1, sx2, sy2, fill=TRAVEL,
                                  dash=(4, 3), width=1)
            elif show_lines:
                color = INFILL if is_infill else PALETTE[pi % len(PALETTE)]
                c.create_line(sx1, sy1, sx2, sy2, fill=color, width=1,
                              arrow=(tk.LAST if arrows_ok else None))

        # 점 & 시작점 (reveal 까지)
        if self.show_points.get() or self.show_start.get():
            self._draw_vertices(reveal)

        # 재생 중: 현재 그리는 지점을 따라가는 강조 마커
        if self._animating:
            self._draw_cursor(reveal)

        # 원점 표시
        self._draw_origin(cw, ch)

        # 배율 표시
        b = self._bounds()
        if b:
            minx, miny, maxx, maxy = b
            c.create_text(8, ch - 6, anchor=tk.SW, fill="#66707a",
                          font=("Consolas", 8),
                          text=f"범위 X[{minx:.1f}, {maxx:.1f}]  Y[{miny:.1f}, {maxy:.1f}]"
                               f"   배율 x{self.scale:.3f}")

    def _kind_of(self, pi):
        """경로 인덱스 pi 의 종류('outline'|'infill'). 안전하게 조회."""
        if 0 <= pi < len(self.path_kinds):
            return self.path_kinds[pi]
        return "outline"

    def _draw_vertices(self, reveal):
        """정점(점)과 시작점 마커를 순서대로 reveal 까지 그린다."""
        c = self.canvas
        # 정점은 세그먼트보다 하나 많다 (첫 정점 포함). reveal 세그먼트 → reveal+1 정점.
        limit = reveal + 1
        show_pt = self.show_points.get()
        show_start = self.show_start.get()
        show_infill = self.show_infill.get()
        n = min(limit, len(self._verts))
        for i in range(n):
            x, y, is_start, pi = self._verts[i]
            is_infill = self._kind_of(pi) == "infill"
            if is_infill and not show_infill:
                continue
            sx, sy = self.w2s(x, y)
            color = INFILL if is_infill else PALETTE[pi % len(PALETTE)]
            # 시작점 번호는 외곽선에만 (채우기는 선이 많아 생략)
            if is_start and show_start and not is_infill:
                c.create_oval(sx - 4, sy - 4, sx + 4, sy + 4,
                              outline=START_MARK, width=1)
                c.create_text(sx + 7, sy - 7, text=str(pi + 1),
                              fill=START_MARK, font=("Consolas", 8), anchor=tk.W)
            if show_pt:
                c.create_oval(sx - 1.5, sy - 1.5, sx + 1.5, sy + 1.5,
                              fill=color, outline="")

    def _draw_cursor(self, reveal):
        """재생 중 현재 그리는 지점을 강조하는 마커(링+점+십자)를 그린다."""
        if not self._verts:
            return
        idx = min(reveal, len(self._verts) - 1)
        x, y, _is_start, _pi = self._verts[idx]
        sx, sy = self.w2s(x, y)
        c = self.canvas
        r = 9
        c.create_oval(sx - r, sy - r, sx + r, sy + r, outline=CURSOR, width=2)
        c.create_line(sx - r - 4, sy, sx - r + 2, sy, fill=CURSOR, width=1)
        c.create_line(sx + r - 2, sy, sx + r + 4, sy, fill=CURSOR, width=1)
        c.create_line(sx, sy - r - 4, sx, sy - r + 2, fill=CURSOR, width=1)
        c.create_line(sx, sy + r - 2, sx, sy + r + 4, fill=CURSOR, width=1)
        c.create_oval(sx - 2.5, sy - 2.5, sx + 2.5, sy + 2.5, fill=CURSOR, outline="")

    def _draw_grid(self, cw, ch):
        c = self.canvas
        # 화면에 보이는 world 범위
        wx0, wy1 = self.s2w(0, 0)         # 좌상단
        wx1, wy0 = self.s2w(cw, ch)       # 우하단
        # 최소 60px 간격이 되도록 world step 선택
        target_px = 70
        raw = target_px / max(self.scale, 1e-9)
        step = nice_step(raw)
        # 세로선 (일정 x)
        import math
        start_i = math.floor(wx0 / step)
        end_i = math.ceil(wx1 / step)
        for i in range(start_i, end_i + 1):
            wx = i * step
            sx, _ = self.w2s(wx, 0)
            color = AXIS if i == 0 else (GRID_MAJOR if i % 5 == 0 else GRID)
            c.create_line(sx, 0, sx, ch, fill=color)
        # 가로선 (일정 y)
        start_j = math.floor(wy0 / step)
        end_j = math.ceil(wy1 / step)
        for j in range(start_j, end_j + 1):
            wy = j * step
            _, sy = self.w2s(0, wy)
            color = AXIS if j == 0 else (GRID_MAJOR if j % 5 == 0 else GRID)
            c.create_line(0, sy, cw, sy, fill=color)

    def _draw_origin(self, cw, ch):
        c = self.canvas
        sx, sy = self.w2s(0, 0)
        if -20 <= sx <= cw + 20 and -20 <= sy <= ch + 20:
            c.create_line(sx - 8, sy, sx + 8, sy, fill=ORIGIN, width=1)
            c.create_line(sx, sy - 8, sx, sy + 8, fill=ORIGIN, width=1)
            c.create_text(sx + 10, sy + 10, text="0,0", fill=ORIGIN,
                          font=("Consolas", 8), anchor=tk.NW)

    # ------------------------------------------------- 출력 미리보기 / 저장
    def _compose_output(self):
        """헤더 + G-code 본문을 합쳐 저장/미리보기용 최종 텍스트를 만든다."""
        header = self.header.get("1.0", "end-1c")   # 마지막 자동 개행 제외
        if header.strip():
            return header.rstrip("\n") + "\n" + self._body
        return self._body

    def _refresh_output(self):
        """헤더 변경/재생성 시 출력 미리보기 텍스트를 다시 채운다."""
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", self._compose_output())

    def save_points(self):
        if not self.paths:
            messagebox.showinfo("알림", "저장할 경로가 없습니다.")
            return
        default = "output.gcode"
        if self.current_file:
            base = os.path.splitext(os.path.basename(self.current_file))[0]
            default = base + ".gcode"
        fpath = filedialog.asksaveasfilename(
            title="G-code 저장", defaultextension=".gcode",
            initialfile=default,
            filetypes=[("G-code 파일", "*.gcode"), ("텍스트 파일", "*.txt"),
                       ("모든 파일", "*.*")])
        if not fpath:
            return
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(self._compose_output())
        self.status.set(f"저장 완료: {fpath}")


def main():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    DxfPointApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
