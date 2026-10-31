import os
import sys
import json
import time
import struct
import tempfile
import datetime
import subprocess
import ctypes
from ctypes import wintypes
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import cv2
import numpy as np
from PIL import Image, ImageTk
import twain

# -------------------------------------------------------------
# 1. Path & Settings Management
# -------------------------------------------------------------
def get_app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

APP_DIR = get_app_dir()
SETTINGS_FILE = os.path.join(APP_DIR, "settings.txt")

DEFAULT_SETTINGS = {
    "twain_source": "",
    "palette": "Monochrome (Raw)",
    "window_center": 32768,
    "window_width": 20000,
    "gamma": 1.0,
    "invert": True,
    "latitude_reduction": 0,
    "edge_enhancement": 0,
    "edge_frequency": 2,
    "noise_reduction": 0,
    "clahe_enabled": False,
    "clahe_clip": 2.0,
    "pixel_pitch_um": 35.0,
    "last_saved_dir": os.path.expanduser("~")
}

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                data = json.load(f)
                settings = DEFAULT_SETTINGS.copy()
                settings.update(data)
                return settings
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()

def save_settings(settings_dict):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings_dict, f, indent=4)
    except Exception as e:
        print(f"Failed to save settings: {e}")

CURRENT_SETTINGS = load_settings()

for p in [r"C:\Windows\twain_32\FireCRDental", r"C:\HERADentVet 3.0", r"C:\heradentvet3.0"]:
    if os.path.exists(p):
        os.environ["PATH"] = p + ";" + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(p)
            except Exception:
                pass

# -------------------------------------------------------------
# 2. Global State Variables
# -------------------------------------------------------------
current_raw_16bit = None
processed_full_image = None
raw_min = 0.0
raw_max = 65535.0
cached_hist_norm = None
is_scanning = False

# Canvas Viewport & Caliper State
zoom_scale = 1.0
offset_x = 0.0
offset_y = 0.0
pan_start_x = 0
pan_start_y = 0
is_panning = False

measuring = False
meas_start_img = None
meas_current_img = None

# Interactive Window/Level Mouse Drag State
is_wl_dragging = False
wl_drag_start_x = 0
wl_drag_start_y = 0
wl_drag_start_center = 32768
wl_drag_start_width = 20000

last_saved_path = None
temp_bmp_path = os.path.join(tempfile.gettempdir(), "firecr_temp_scan.bmp")

COLORMAP_MODES = {
    "Monochrome (Raw)": None,
    "X-Ray Teardown (Jet)": cv2.COLORMAP_JET,
    "Turbo (Perceptual)": cv2.COLORMAP_TURBO,
    "Inferno (Thermal)": cv2.COLORMAP_INFERNO,
    "Ocean (Clinical Blue)": cv2.COLORMAP_OCEAN,
    "Hot (Black to Red/Yellow)": cv2.COLORMAP_HOT
}

# -------------------------------------------------------------
# 3. TWAIN Acquisition & Pipeline
# -------------------------------------------------------------
def find_firecr_source(sm):
    try:
        sources = sm.source_list
    except Exception:
        return None
    if not sources:
        return None

    saved = CURRENT_SETTINGS.get("twain_source")
    if saved and saved in sources:
        return saved

    for src in sources:
        if "firecr" in src.lower():
            return src
    for src in sources:
        if any(kw in src.lower() for kw in ["digiray", "quantor", "3disc", "crswing"]):
            return src

    non_wia = [s for s in sources if not s.lower().startswith("wia-") and "hp" not in s.lower()]
    return non_wia[0] if len(non_wia) == 1 else None

def load_raw_twain_bmp(file_path):
    with open(file_path, "rb") as f:
        bmp_header = f.read(14)
        magic, _, _, _, offset = struct.unpack("<2sIHHI", bmp_header)
        if magic != b"BM":
            raise ValueError("Invalid bitmap buffer.")

        dib_header = f.read(40)
        (biSize, biWidth, biHeight, biPlanes, biBitCount,
         biCompression, biSizeImage, biXPelsPerMeter,
         biYPelsPerMeter, biClrUsed, biClrImportant) = struct.unpack("<IIIHHIIIIII", dib_header)

        width = biWidth
        height = abs(biHeight)
        is_bottom_up = (biHeight > 0)
        f.seek(offset)

        if biBitCount == 16:
            row_stride = ((width * 2 + 3) // 4) * 4
            raw_bytes = f.read(row_stride * height)
            raw_array = np.frombuffer(raw_bytes, dtype=np.uint8).reshape((height, row_stride))
            img = raw_array[:, :width * 2].copy().view(np.uint16).reshape((height, width))
            if is_bottom_up:
                img = np.flipud(img)
            return img
        elif biBitCount == 8:
            row_stride = ((width + 3) // 4) * 4
            raw_bytes = f.read(row_stride * height)
            raw_array = np.frombuffer(raw_bytes, dtype=np.uint8).reshape((height, row_stride))
            img = raw_array[:, :width].copy()
            if is_bottom_up:
                img = np.flipud(img)
            return img
        return cv2.imread(file_path, cv2.IMREAD_UNCHANGED)

def compute_histogram(raw_img):
    global cached_hist_norm
    if raw_img is None:
        cached_hist_norm = None
        return
    hist, _ = np.histogram(raw_img, bins=256, range=(0, 65535))
    hist_log = np.log1p(hist)
    max_val = max(hist_log.max(), 1.0)
    cached_hist_norm = (hist_log / max_val * 38.0).astype(int)

def draw_histogram_overlay():
    if is_scanning:
        return
    hist_canvas.delete("all")
    hw = hist_canvas.winfo_width() or 270
    hh = hist_canvas.winfo_height() or 40

    if cached_hist_norm is None:
        hist_canvas.create_text(hw // 2, hh // 2, text="No Image Histogram", fill="#666666", font=("Arial", 8))
        return

    pts = [0, hh]
    for i in range(256):
        x = int(i / 255.0 * hw)
        y = hh - cached_hist_norm[i]
        pts.extend([x, y])
    pts.extend([hw, hh])
    hist_canvas.create_polygon(pts, fill="#2b485d", outline="#4f7f9e")

    wc = wl_center_slider.get()
    ww = wl_width_slider.get()

    x_min = int(np.clip((wc - ww / 2.0) / 65535.0 * hw, 0, hw))
    x_max = int(np.clip((wc + ww / 2.0) / 65535.0 * hw, 0, hw))
    x_mid = int(np.clip(wc / 65535.0 * hw, 0, hw))

    hist_canvas.create_rectangle(x_min, 0, x_max, hh, fill="#00ffff", stipple="gray25", outline="#00ffff")
    hist_canvas.create_line(x_mid, 0, x_mid, hh, fill="#ffaa00", width=2)

    hist_canvas.create_text(6, 8, anchor="w", text=f"L: {int(wc)}", fill="#ffaa00", font=("Arial", 7, "bold"))
    hist_canvas.create_text(hw - 6, 8, anchor="e", text=f"W: {int(ww)}", fill="#00ffff", font=("Arial", 7, "bold"))

def process_image_pipeline(raw_img):
    if raw_img is None:
        return None

    wc = wl_center_slider.get()
    ww = max(wl_width_slider.get(), 100.0)
    gamma = gamma_slider.get()
    invert = invert_var.get()
    lat_red = lat_slider.get()
    edge_enh = edge_enh_slider.get()
    edge_freq = edge_freq_slider.get()
    noise_red = noise_slider.get()
    clahe_en = clahe_var.get()
    clahe_clip = clahe_slider.get()
    cmap_choice = cmap_var.get()

    raw_x = np.arange(65536, dtype=np.float32)
    low_bound = wc - (ww / 2.0)
    norm = np.clip((raw_x - low_bound) / ww, 0.0, 1.0)

    if gamma != 1.0:
        norm = np.power(norm, 1.0 / max(gamma, 0.01))
    if invert:
        norm = 1.0 - norm

    lut_8 = np.clip(norm * 255.0 + 0.5, 0, 255).astype(np.uint8)
    base_8 = lut_8[raw_img]

    if lat_red > 0:
        alpha = lat_red * 0.05
        blur_low = cv2.GaussianBlur(base_8, (0, 0), sigmaX=32)
        base_8 = cv2.addWeighted(base_8, 1.0, blur_low, -alpha, int(128 * alpha))

    if noise_red > 0:
        base_8 = cv2.bilateralFilter(base_8, d=5, sigmaColor=noise_red * 5, sigmaSpace=noise_red * 2)

    if edge_enh > 0:
        sigma = 0.5 * edge_freq
        blurred = cv2.GaussianBlur(base_8, (0, 0), sigmaX=sigma)
        gain = edge_enh * 0.25
        base_8 = cv2.addWeighted(base_8, 1.0 + gain, blurred, -gain, 0)

    if clahe_en:
        clahe = cv2.createCLAHE(clipLimit=max(clahe_clip, 0.1), tileGridSize=(8, 8))
        base_8 = clahe.apply(base_8)

    cmap = COLORMAP_MODES.get(cmap_choice)
    if cmap is not None:
        color_bgr = cv2.applyColorMap(base_8, cmap)
        return cv2.cvtColor(color_bgr, cv2.COLOR_BGR2RGB)
    return base_8

def update_processed_image(*args):
    global processed_full_image
    if current_raw_16bit is None or is_scanning:
        return
    processed_full_image = process_image_pipeline(current_raw_16bit)
    draw_histogram_overlay()
    redraw_canvas()

# -------------------------------------------------------------
# 4. Canvas Viewport, Zoom, Pan & Caliper Drawing
# -------------------------------------------------------------
def redraw_canvas():
    global processed_full_image, zoom_scale, offset_x, offset_y
    if is_scanning:
        return

    canvas.delete("img")
    canvas.delete("caliper")

    if processed_full_image is None:
        canvas.create_text(canvas.winfo_width() // 2, canvas.winfo_height() // 2,
                           text="[No Plate Scanned]", fill="#666666", font=("Arial", 12), tags="img")
        return

    cw = max(canvas.winfo_width(), 100)
    ch = max(canvas.winfo_height(), 100)
    ih, iw = processed_full_image.shape[:2]

    src_x0 = max(0, int(-offset_x / zoom_scale))
    src_y0 = max(0, int(-offset_y / zoom_scale))
    src_x1 = min(iw, int(np.ceil((cw - offset_x) / zoom_scale)))
    src_y1 = min(ih, int(np.ceil((ch - offset_y) / zoom_scale)))

    if src_x1 <= src_x0 or src_y1 <= src_y0:
        return

    dst_x = int(offset_x + src_x0 * zoom_scale)
    dst_y = int(offset_y + src_y0 * zoom_scale)
    dst_w = max(1, int(round((src_x1 - src_x0) * zoom_scale)))
    dst_h = max(1, int(round((src_y1 - src_y0) * zoom_scale)))

    cropped = processed_full_image[src_y0:src_y1, src_x0:src_x1]
    resized = cv2.resize(cropped, (dst_w, dst_h), interpolation=cv2.INTER_NEAREST if zoom_scale > 2.0 else cv2.INTER_AREA)

    img_pil = Image.fromarray(resized)
    canvas.img_tk = ImageTk.PhotoImage(img_pil)
    canvas.create_image(dst_x, dst_y, anchor=tk.NW, image=canvas.img_tk, tags="img")

    draw_caliper_overlay()

def fit_to_window():
    global zoom_scale, offset_x, offset_y
    if processed_full_image is None:
        return
    cw = max(canvas.winfo_width(), 100)
    ch = max(canvas.winfo_height(), 100)
    ih, iw = processed_full_image.shape[:2]
    zoom_scale = min(cw / iw, ch / ih)
    offset_x = (cw - iw * zoom_scale) / 2.0
    offset_y = (ch - ih * zoom_scale) / 2.0
    redraw_canvas()

def zoom_1to1():
    global zoom_scale, offset_x, offset_y
    if processed_full_image is None:
        return
    cw = max(canvas.winfo_width(), 100)
    ch = max(canvas.winfo_height(), 100)
    ih, iw = processed_full_image.shape[:2]
    zoom_scale = 1.0
    offset_x = (cw - iw) / 2.0
    offset_y = (ch - ih) / 2.0
    redraw_canvas()

def on_mouse_wheel(event):
    global zoom_scale, offset_x, offset_y
    if processed_full_image is None or is_scanning:
        return
    factor = 1.25 if (event.delta > 0 or event.num == 4) else 0.8
    new_scale = max(0.05, min(zoom_scale * factor, 15.0))

    mx, my = event.x, event.y
    ix = (mx - offset_x) / zoom_scale
    iy = (my - offset_y) / zoom_scale

    offset_x = mx - ix * new_scale
    offset_y = my - iy * new_scale
    zoom_scale = new_scale
    redraw_canvas()

def canvas_to_img_coords(cx, cy):
    if processed_full_image is None or zoom_scale == 0:
        return 0, 0
    ix = int((cx - offset_x) / zoom_scale)
    iy = int((cy - offset_y) / zoom_scale)
    ih, iw = processed_full_image.shape[:2]
    return max(0, min(ix, iw - 1)), max(0, min(iy, ih - 1))

def img_to_canvas_coords(ix, iy):
    return offset_x + ix * zoom_scale, offset_y + iy * zoom_scale

def on_mouse_move(event):
    if current_raw_16bit is None or is_scanning:
        return
    ix, iy = canvas_to_img_coords(event.x, event.y)
    raw_val = current_raw_16bit[iy, ix]
    cursor_info_lbl.config(text=f"Pixel: ({ix}, {iy}) | Raw 16-bit: {raw_val}")

# -------------------------------------------------------------
# 5. Mouse Interactions (Pan, Caliper, Window/Level Drag)
# -------------------------------------------------------------
def on_left_press(event):
    global pan_start_x, pan_start_y, is_panning, measuring, meas_start_img, meas_current_img
    global is_wl_dragging, wl_drag_start_x, wl_drag_start_y, wl_drag_start_center, wl_drag_start_width
    if is_scanning:
        return

    if event.state & 0x0004:
        is_wl_dragging = True
        wl_drag_start_x = event.x
        wl_drag_start_y = event.y
        wl_drag_start_center = wl_center_slider.get()
        wl_drag_start_width = wl_width_slider.get()
    elif caliper_tool_var.get():
        measuring = True
        meas_start_img = canvas_to_img_coords(event.x, event.y)
        meas_current_img = meas_start_img
        redraw_canvas()
    else:
        is_panning = True
        pan_start_x = event.x - offset_x
        pan_start_y = event.y - offset_y

def on_left_drag(event):
    global offset_x, offset_y, meas_current_img
    if is_scanning:
        return
    if is_wl_dragging:
        handle_wl_mouse_motion(event.x, event.y)
    elif caliper_tool_var.get() and measuring:
        meas_current_img = canvas_to_img_coords(event.x, event.y)
        redraw_canvas()
    elif is_panning:
        offset_x = event.x - pan_start_x
        offset_y = event.y - pan_start_y
        redraw_canvas()

def on_left_release(event):
    global is_panning, measuring, is_wl_dragging
    is_panning = False
    is_wl_dragging = False
    if measuring:
        measuring = False
        redraw_canvas()

def on_middle_press(event):
    global is_wl_dragging, wl_drag_start_x, wl_drag_start_y, wl_drag_start_center, wl_drag_start_width
    if is_scanning:
        return
    is_wl_dragging = True
    wl_drag_start_x = event.x
    wl_drag_start_y = event.y
    wl_drag_start_center = wl_center_slider.get()
    wl_drag_start_width = wl_width_slider.get()

def on_middle_drag(event):
    if is_wl_dragging and not is_scanning:
        handle_wl_mouse_motion(event.x, event.y)

def on_middle_release(event):
    global is_wl_dragging
    is_wl_dragging = False

def handle_wl_mouse_motion(curr_x, curr_y):
    dx = curr_x - wl_drag_start_x
    dy = curr_y - wl_drag_start_y

    new_w = max(100.0, min(65535.0, wl_drag_start_width + (dx * 45.0)))
    new_c = max(0.0, min(65535.0, wl_drag_start_center - (dy * 45.0)))

    wl_width_slider.set(new_w)
    wl_center_slider.set(new_c)
    update_processed_image()

def on_right_press(event):
    global measuring, meas_start_img, meas_current_img
    if is_scanning:
        return
    measuring = True
    meas_start_img = canvas_to_img_coords(event.x, event.y)
    meas_current_img = meas_start_img
    redraw_canvas()

def on_right_drag(event):
    global meas_current_img
    if measuring and not is_scanning:
        meas_current_img = canvas_to_img_coords(event.x, event.y)
        redraw_canvas()

def on_right_release(event):
    global measuring
    measuring = False
    redraw_canvas()

def clear_caliper():
    global meas_start_img, meas_current_img
    meas_start_img = None
    meas_current_img = None
    meas_info_lbl.config(text="Caliper: None")
    redraw_canvas()

def draw_caliper_overlay():
    if meas_start_img is None or meas_current_img is None:
        return
    x1, y1 = img_to_canvas_coords(*meas_start_img)
    x2, y2 = img_to_canvas_coords(*meas_current_img)

    dx_px = meas_current_img[0] - meas_start_img[0]
    dy_px = meas_current_img[1] - meas_start_img[1]
    dist_px = np.sqrt(dx_px**2 + dy_px**2)

    try:
        pitch_um = float(pitch_entry.get())
    except ValueError:
        pitch_um = 35.0

    dist_mm = dist_px * (pitch_um / 1000.0)
    dist_mils = dist_mm * 39.3701

    canvas.create_line(x1, y1, x2, y2, fill="#00ffcc", width=2, tags="caliper")
    for px, py in [(x1, y1), (x2, y2)]:
        canvas.create_oval(px-3, py-3, px+3, py+3, fill="#ffcc00", outline="black", tags="caliper")

    mid_x, mid_y = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    text_str = f" {dist_mm:.2f} mm ({dist_mils:.1f} mil) "
    canvas.create_rectangle(mid_x - 45, mid_y - 12, mid_x + 45, mid_y + 12, fill="#111111", outline="#00ffcc", tags="caliper")
    canvas.create_text(mid_x, mid_y, text=text_str, fill="#ffffff", font=("Arial", 9, "bold"), tags="caliper")

    meas_info_lbl.config(text=f"Caliper: {dist_mm:.3f} mm | {dist_mils:.1f} mil | {dist_px:.1f} px")

# -------------------------------------------------------------
# 6. Acquisition & Preset Routines
# -------------------------------------------------------------
def scan_plate(force_dialog=False):
    global current_raw_16bit, raw_min, raw_max, is_scanning
    sm = None
    sd = None
    hwnd = root.winfo_id()

    try:
        status_lbl.config(text="Connecting to FireCR...")
        root.update()

        legacy_dsm = r"C:\Windows\twain_32.dll"
        sm = twain.SourceManager(hwnd, dsm_name=legacy_dsm) if os.path.exists(legacy_dsm) else twain.SourceManager(hwnd)

        target_src = None if force_dialog else find_firecr_source(sm)

        if target_src:
            sd = sm.open_source(target_src)
            if CURRENT_SETTINGS.get("twain_source") != target_src:
                CURRENT_SETTINGS["twain_source"] = target_src
                save_settings(CURRENT_SETTINGS)
        else:
            sd = sm.open_source()

        if not sd:
            status_lbl.config(text="Device selection cancelled.")
            return

        status_lbl.config(text="FireCR active. Feed plate...")
        root.update()

        is_scanning = True
        try:
            # Native, standard acquisition without message-loop interception
            sd.request_acquire(show_ui=True, modal_ui=True)
            sd.modal_loop()
        finally:
            is_scanning = False

        try:
            (handle, count) = sd.xfer_image_natively()
        except Exception:
            status_lbl.config(text="Scan cancelled or window closed.")
            return

        status_lbl.config(text="Processing raw 16-bit radiograph...")
        root.update()

        save_func = getattr(twain, 'DIBToBMFile', getattr(twain, 'dib_to_bm_file', None))
        save_func(handle, temp_bmp_path)

        current_raw_16bit = load_raw_twain_bmp(temp_bmp_path)
        if current_raw_16bit is None:
            messagebox.showerror("Error", "Could not parse image buffer.")
            return

        raw_min = float(np.percentile(current_raw_16bit, 0.1))
        raw_max = float(np.percentile(current_raw_16bit, 99.9))
        if raw_max <= raw_min:
            raw_min = float(current_raw_16bit.min())
            raw_max = float(current_raw_16bit.max())

        mid_point = (raw_min + raw_max) / 2.0
        span = max(raw_max - raw_min, 1000.0)
        wl_center_slider.set(mid_point)
        wl_width_slider.set(span)

        compute_histogram(current_raw_16bit)

        h, w = current_raw_16bit.shape[:2]
        status_lbl.config(text=f"Loaded: 16-bit ({w}x{h}) | Range: [{int(raw_min)} - {int(raw_max)}]")
        update_processed_image()
        fit_to_window()

    except Exception as e:
        status_lbl.config(text="Ready")
    finally:
        is_scanning = False
        if sd:
            try: sd.destroy()
            except Exception: pass
        if sm:
            try: sm.destroy()
            except Exception: pass

def rotate_plate():
    global current_raw_16bit
    if current_raw_16bit is None or is_scanning:
        return
    current_raw_16bit = cv2.rotate(current_raw_16bit, cv2.ROTATE_90_CLOCKWISE)
    compute_histogram(current_raw_16bit)
    update_processed_image()
    fit_to_window()

def apply_preset(mode):
    if is_scanning:
        return
    if mode == "linear":
        if current_raw_16bit is not None:
            wl_center_slider.set((raw_min + raw_max) / 2.0)
            wl_width_slider.set(max(raw_max - raw_min, 1000.0))
        lat_slider.set(0)
        edge_enh_slider.set(0)
        edge_freq_slider.set(2)
        noise_slider.set(0)
        clahe_var.set(False)
        cmap_var.set("Monochrome (Raw)")
        invert_var.set(True)
        gamma_slider.set(1.0)
    elif mode == "wire_bond":
        if current_raw_16bit is not None:
            wl_center_slider.set(raw_min + (raw_max - raw_min) * 0.45)
            wl_width_slider.set(max((raw_max - raw_min) * 0.5, 4000.0))
        lat_slider.set(4)
        edge_enh_slider.set(6)
        edge_freq_slider.set(1)
        noise_slider.set(2)
        clahe_var.set(True)
        clahe_slider.set(2.5)
        cmap_var.set("Monochrome (Raw)")
        invert_var.set(True)
        gamma_slider.set(1.1)
    elif mode == "solder_void":
        if current_raw_16bit is not None:
            wl_center_slider.set(raw_min + (raw_max - raw_min) * 0.65)
            wl_width_slider.set(max((raw_max - raw_min) * 0.4, 5000.0))
        lat_slider.set(6)
        edge_enh_slider.set(5)
        edge_freq_slider.set(3)
        noise_slider.set(2)
        clahe_var.set(True)
        clahe_slider.set(2.0)
        cmap_var.set("X-Ray Teardown (Jet)")
        invert_var.set(True)
        gamma_slider.set(1.0)
    elif mode == "bga_struct":
        if current_raw_16bit is not None:
            wl_center_slider.set((raw_min + raw_max) / 2.0)
            wl_width_slider.set(max((raw_max - raw_min) * 0.8, 8000.0))
        lat_slider.set(8)
        edge_enh_slider.set(4)
        edge_freq_slider.set(4)
        noise_slider.set(3)
        clahe_var.set(False)
        cmap_var.set("Turbo (Perceptual)")
        invert_var.set(True)
        gamma_slider.set(1.2)
    update_processed_image()

def save_image():
    global last_saved_path
    if current_raw_16bit is None or processed_full_image is None or is_scanning:
        messagebox.showwarning("Warning", "Scan a plate first.")
        return

    now = datetime.datetime.now()
    default_name = now.strftime("%d%m%y-%H%M%S.tiff")

    saved_dir = CURRENT_SETTINGS.get("last_saved_dir", os.path.expanduser("~"))
    file_path = filedialog.asksaveasfilename(
        initialdir=saved_dir,
        initialfile=default_name,
        defaultextension=".tiff",
        filetypes=[("TIFF Image (*.tiff;*.tif)", "*.tiff;*.tif"), ("PNG Image (*.png)", "*.png")]
    )
    if file_path:
        if len(processed_full_image.shape) == 3:
            bgr = cv2.cvtColor(processed_full_image, cv2.COLOR_RGB2BGR)
            cv2.imwrite(file_path, bgr)
        else:
            if file_path.lower().endswith(".png"):
                cv2.imwrite(file_path, processed_full_image)
            else:
                wc = wl_center_slider.get()
                ww = max(wl_width_slider.get(), 100.0)
                gamma = gamma_slider.get()
                invert = invert_var.get()

                raw_x = np.arange(65536, dtype=np.float32)
                low_bound = wc - (ww / 2.0)
                norm = np.clip((raw_x - low_bound) / ww, 0.0, 1.0)
                if gamma != 1.0:
                    norm = np.power(norm, 1.0 / max(gamma, 0.01))
                if invert:
                    norm = 1.0 - norm
                lut_16 = np.clip(norm * 65535.0 + 0.5, 0, 65535).astype(np.uint16)
                final_16 = lut_16[current_raw_16bit]
                cv2.imwrite(file_path, final_16)

        last_saved_path = file_path
        CURRENT_SETTINGS["last_saved_dir"] = os.path.dirname(file_path)
        save_settings(CURRENT_SETTINGS)
        status_lbl.config(text=f"Saved: {os.path.basename(file_path)}")

def open_save_folder():
    saved_dir = CURRENT_SETTINGS.get("last_saved_dir", os.path.expanduser("~"))
    if last_saved_path and os.path.exists(last_saved_path):
        subprocess.Popen(f'explorer /select,"{os.path.normpath(last_saved_path)}"')
    elif os.path.exists(saved_dir):
        os.startfile(os.path.normpath(saved_dir))

def save_current_as_defaults():
    CURRENT_SETTINGS["palette"] = cmap_var.get()
    CURRENT_SETTINGS["window_center"] = int(wl_center_slider.get())
    CURRENT_SETTINGS["window_width"] = int(wl_width_slider.get())
    CURRENT_SETTINGS["gamma"] = gamma_slider.get()
    CURRENT_SETTINGS["invert"] = invert_var.get()
    CURRENT_SETTINGS["latitude_reduction"] = int(lat_slider.get())
    CURRENT_SETTINGS["edge_enhancement"] = int(edge_enh_slider.get())
    CURRENT_SETTINGS["edge_frequency"] = int(edge_freq_slider.get())
    CURRENT_SETTINGS["noise_reduction"] = int(noise_slider.get())
    CURRENT_SETTINGS["clahe_enabled"] = clahe_var.get()
    CURRENT_SETTINGS["clahe_clip"] = clahe_slider.get()
    try:
        CURRENT_SETTINGS["pixel_pitch_um"] = float(pitch_entry.get())
    except ValueError:
        pass
    save_settings(CURRENT_SETTINGS)
    messagebox.showinfo("Settings", f"Settings saved to:\n{SETTINGS_FILE}")

# -------------------------------------------------------------
# 7. GUI Layout Construction
# -------------------------------------------------------------
root = tk.Tk()
root.title("FireCR 16-bit Radiography & Electronics Inspection - V5.0")

# Top Action Toolbar
toolbar = tk.Frame(root, bg="#2a2a2a", pady=5)
toolbar.pack(fill=tk.X)

tk.Button(toolbar, text="Acquire from FireCR", font=("Arial", 10, "bold"), bg="#2b579a", fg="white", padx=12, pady=2, command=lambda: scan_plate(force_dialog=False)).pack(side=tk.LEFT, padx=5)
tk.Button(toolbar, text="Rotate 90°", font=("Arial", 9, "bold"), bg="#4a4a4a", fg="white", padx=8, pady=2, command=rotate_plate).pack(side=tk.LEFT, padx=3)
tk.Button(toolbar, text="Fit View", font=("Arial", 9), bg="#4a4a4a", fg="white", padx=8, pady=2, command=fit_to_window).pack(side=tk.LEFT, padx=3)
tk.Button(toolbar, text="1:1 Native Pixels", font=("Arial", 9), bg="#4a4a4a", fg="white", padx=8, pady=2, command=zoom_1to1).pack(side=tk.LEFT, padx=3)

caliper_tool_var = tk.BooleanVar(value=False)
tk.Checkbutton(toolbar, text="Caliper Tool", variable=caliper_tool_var, font=("Arial", 9, "bold"), bg="#ffaa00", selectcolor="#2a2a2a", indicatoron=False, padx=8, pady=2).pack(side=tk.LEFT, padx=6)
tk.Button(toolbar, text="Clear Measure", font=("Arial", 8), bg="#3d3d3d", fg="white", padx=6, pady=2, command=clear_caliper).pack(side=tk.LEFT, padx=3)

tk.Button(toolbar, text="Open Folder", font=("Arial", 9), bg="#3d3d3d", fg="white", padx=8, pady=2, command=open_save_folder).pack(side=tk.RIGHT, padx=5)
tk.Button(toolbar, text="Save Image...", font=("Arial", 10, "bold"), bg="#107c41", fg="white", padx=12, pady=2, command=save_image).pack(side=tk.RIGHT, padx=5)

# Main Workspace
main_frame = tk.Frame(root)
main_frame.pack(fill=tk.BOTH, expand=True)

# Right Sidebar Container
sidebar_container = tk.Frame(main_frame, width=320, bg="#f5f5f5")
sidebar_container.pack(side=tk.RIGHT, fill=tk.Y)
sidebar_container.pack_propagate(False)

scrollbar = ttk.Scrollbar(sidebar_container, orient="vertical")

panel_canvas = tk.Canvas(sidebar_container, bg="#f5f5f5", highlightthickness=0, yscrollcommand=scrollbar.set)
panel_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
scrollbar.config(command=panel_canvas.yview)

panel = tk.Frame(panel_canvas, bg="#f5f5f5", padx=6, pady=2)
canvas_window = panel_canvas.create_window((0, 0), window=panel, anchor="nw")

def on_canvas_configure(event):
    if not is_scanning:
        panel_canvas.itemconfig(canvas_window, width=event.width)

def on_panel_configure(event):
    if not is_scanning:
        bbox = panel_canvas.bbox("all")
        panel_canvas.configure(scrollregion=bbox)
        # Auto-hide scrollbar if sidebar content fits completely
        if bbox:
            content_h = bbox[3] - bbox[1]
            visible_h = panel_canvas.winfo_height()
            if content_h <= visible_h:
                if scrollbar.winfo_ismapped():
                    scrollbar.pack_forget()
            else:
                if not scrollbar.winfo_ismapped():
                    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

panel_canvas.bind("<Configure>", on_canvas_configure)
panel.bind("<Configure>", on_panel_configure)

def on_panel_mousewheel(event):
    if event.delta and not is_scanning:
        panel_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

panel_canvas.bind("<Enter>", lambda e: panel_canvas.bind_all("<MouseWheel>", on_panel_mousewheel))
panel_canvas.bind("<Leave>", lambda e: panel_canvas.unbind_all("<MouseWheel>"))

# Left Canvas Viewport
canvas_frame = tk.Frame(main_frame, bg="#121212")
canvas_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

canvas = tk.Canvas(canvas_frame, bg="#121212", highlightthickness=0)
canvas.pack(fill=tk.BOTH, expand=True)

canvas.bind("<Configure>", lambda e: redraw_canvas())
canvas.bind("<Motion>", on_mouse_move)

canvas.bind("<ButtonPress-1>", on_left_press)
canvas.bind("<B1-Motion>", on_left_drag)
canvas.bind("<ButtonRelease-1>", on_left_release)

canvas.bind("<ButtonPress-2>", on_middle_press)
canvas.bind("<B2-Motion>", on_middle_drag)
canvas.bind("<ButtonRelease-2>", on_middle_release)

canvas.bind("<ButtonPress-3>", on_right_press)
canvas.bind("<B3-Motion>", on_right_drag)
canvas.bind("<ButtonRelease-3>", on_right_release)

canvas.bind("<MouseWheel>", on_mouse_wheel)
canvas.bind("<Button-4>", on_mouse_wheel)
canvas.bind("<Button-5>", on_mouse_wheel)

# --- Sidebar Section 1: Presets ---
preset_frame = tk.LabelFrame(panel, text="INSPECTION PRESETS", font=("Arial", 8, "bold"), bg="#f5f5f5", padx=4, pady=2)
preset_frame.pack(fill=tk.X, pady=(0, 2))

preset_frame.grid_columnconfigure(0, weight=1)
preset_frame.grid_columnconfigure(1, weight=1)

tk.Button(preset_frame, text="Linear (Raw)", font=("Arial", 8), bg="#e0e0e0", command=lambda: apply_preset("linear")).grid(row=0, column=0, sticky="ew", padx=2, pady=1)
tk.Button(preset_frame, text="Wire Bond & Die", font=("Arial", 8, "bold"), bg="#d9e1f2", command=lambda: apply_preset("wire_bond")).grid(row=0, column=1, sticky="ew", padx=2, pady=1)
tk.Button(preset_frame, text="Solder & Voids", font=("Arial", 8), bg="#ffe6cc", command=lambda: apply_preset("solder_void")).grid(row=1, column=0, sticky="ew", padx=2, pady=1)
tk.Button(preset_frame, text="BGA Structural", font=("Arial", 8), bg="#e2efda", command=lambda: apply_preset("bga_struct")).grid(row=1, column=1, sticky="ew", padx=2, pady=1)

# --- Sidebar Section 2: Radiographic Windowing (WW / WL) ---
wl_frame = tk.LabelFrame(panel, text="RADIOGRAPHIC WINDOWING", font=("Arial", 8, "bold"), bg="#f5f5f5", padx=4, pady=2)
wl_frame.pack(fill=tk.X, pady=2)

tk.Label(wl_frame, text="Log Intensity Histogram & Display Span:", font=("Arial", 7), bg="#f5f5f5").pack(anchor="w")
hist_canvas = tk.Canvas(wl_frame, height=38, bg="#181818", highlightthickness=1, highlightbackground="#333333")
hist_canvas.pack(fill=tk.X, pady=(1, 2))
hist_canvas.bind("<Configure>", lambda e: draw_histogram_overlay())

tk.Label(wl_frame, text="Window Center (Level):", font=("Arial", 7, "bold"), bg="#f5f5f5").pack(anchor="w")
wl_center_slider = tk.Scale(wl_frame, from_=0, to=65535, orient=tk.HORIZONTAL, bg="#f5f5f5", command=update_processed_image)
wl_center_slider.set(CURRENT_SETTINGS.get("window_center", 32768))
wl_center_slider.pack(fill=tk.X)

tk.Label(wl_frame, text="Window Width:", font=("Arial", 7, "bold"), bg="#f5f5f5").pack(anchor="w")
wl_width_slider = tk.Scale(wl_frame, from_=100, to=65535, orient=tk.HORIZONTAL, bg="#f5f5f5", command=update_processed_image)
wl_width_slider.set(CURRENT_SETTINGS.get("window_width", 20000))
wl_width_slider.pack(fill=tk.X)

tk.Label(wl_frame, text="Tip: Drag Middle-Click (or Ctrl+Left) on image to adjust", font=("Arial", 7, "italic"), fg="#555555", bg="#f5f5f5").pack(anchor="w")

# --- Sidebar Section 3: CR Detail & Latitude Filters ---
filter_frame = tk.LabelFrame(panel, text="CR DETAIL & LATITUDE FILTERS", font=("Arial", 8, "bold"), bg="#f5f5f5", padx=4, pady=2)
filter_frame.pack(fill=tk.X, pady=2)

tk.Label(filter_frame, text="Latitude Reduction (Dynamic Compression):", font=("Arial", 7, "bold"), bg="#f5f5f5").pack(anchor="w")
lat_slider = tk.Scale(filter_frame, from_=0, to=10, orient=tk.HORIZONTAL, bg="#f5f5f5", command=update_processed_image)
lat_slider.set(CURRENT_SETTINGS.get("latitude_reduction", 0))
lat_slider.pack(fill=tk.X)

# Compact 2-Column Row: Edge Enhancement + Edge Frequency
edge_grid = tk.Frame(filter_frame, bg="#f5f5f5")
edge_grid.pack(fill=tk.X)
edge_grid.grid_columnconfigure(0, weight=1)
edge_grid.grid_columnconfigure(1, weight=1)

tk.Label(edge_grid, text="Edge Gain:", font=("Arial", 7, "bold"), bg="#f5f5f5").grid(row=0, column=0, sticky="w")
tk.Label(edge_grid, text="Edge Scale (1-5):", font=("Arial", 7, "bold"), bg="#f5f5f5").grid(row=0, column=1, sticky="w")

edge_enh_slider = tk.Scale(edge_grid, from_=0, to=10, orient=tk.HORIZONTAL, bg="#f5f5f5", command=update_processed_image)
edge_enh_slider.set(CURRENT_SETTINGS.get("edge_enhancement", 0))
edge_enh_slider.grid(row=1, column=0, sticky="ew", padx=(0, 2))

edge_freq_slider = tk.Scale(edge_grid, from_=1, to=5, orient=tk.HORIZONTAL, bg="#f5f5f5", command=update_processed_image)
edge_freq_slider.set(CURRENT_SETTINGS.get("edge_frequency", 2))
edge_freq_slider.grid(row=1, column=1, sticky="ew", padx=(2, 0))

tk.Label(filter_frame, text="Noise Reduction (Bilateral):", font=("Arial", 7, "bold"), bg="#f5f5f5").pack(anchor="w")
noise_slider = tk.Scale(filter_frame, from_=0, to=10, orient=tk.HORIZONTAL, bg="#f5f5f5", command=update_processed_image)
noise_slider.set(CURRENT_SETTINGS.get("noise_reduction", 0))
noise_slider.pack(fill=tk.X)

# --- Sidebar Section 4: Display & Tone ---
tone_frame = tk.LabelFrame(panel, text="DISPLAY & TONE", font=("Arial", 8, "bold"), bg="#f5f5f5", padx=4, pady=2)
tone_frame.pack(fill=tk.X, pady=2)

tk.Label(tone_frame, text="Pseudo-Color Palette:", font=("Arial", 7, "bold"), bg="#f5f5f5").pack(anchor="w")
cmap_var = tk.StringVar(value=CURRENT_SETTINGS.get("palette", "Monochrome (Raw)"))
cmap_dropdown = ttk.Combobox(tone_frame, textvariable=cmap_var, values=list(COLORMAP_MODES.keys()), state="readonly")
cmap_dropdown.pack(fill=tk.X, pady=(0, 2))
cmap_dropdown.bind("<<ComboboxSelected>>", update_processed_image)

chk_row = tk.Frame(tone_frame, bg="#f5f5f5")
chk_row.pack(fill=tk.X, pady=1)
invert_var = tk.BooleanVar(value=CURRENT_SETTINGS.get("invert", True))
tk.Checkbutton(chk_row, text="Invert Density", variable=invert_var, bg="#f5f5f5", font=("Arial", 7), command=update_processed_image).pack(side=tk.LEFT)
clahe_var = tk.BooleanVar(value=CURRENT_SETTINGS.get("clahe_enabled", False))
tk.Checkbutton(chk_row, text="Enable CLAHE", variable=clahe_var, bg="#f5f5f5", font=("Arial", 7), command=update_processed_image).pack(side=tk.LEFT, padx=10)

# Compact 2-Column Row: CLAHE Clip + Gamma
tone_grid = tk.Frame(tone_frame, bg="#f5f5f5")
tone_grid.pack(fill=tk.X)
tone_grid.grid_columnconfigure(0, weight=1)
tone_grid.grid_columnconfigure(1, weight=1)

tk.Label(tone_grid, text="CLAHE Clip:", font=("Arial", 7), bg="#f5f5f5").grid(row=0, column=0, sticky="w")
tk.Label(tone_grid, text="Gamma Curve:", font=("Arial", 7), bg="#f5f5f5").grid(row=0, column=1, sticky="w")

clahe_slider = tk.Scale(tone_grid, from_=0.5, to=6.0, resolution=0.1, orient=tk.HORIZONTAL, bg="#f5f5f5", command=update_processed_image)
clahe_slider.set(CURRENT_SETTINGS.get("clahe_clip", 2.0))
clahe_slider.grid(row=1, column=0, sticky="ew", padx=(0, 2))

gamma_slider = tk.Scale(tone_grid, from_=0.2, to=3.0, resolution=0.05, orient=tk.HORIZONTAL, bg="#f5f5f5", command=update_processed_image)
gamma_slider.set(CURRENT_SETTINGS.get("gamma", 1.0))
gamma_slider.grid(row=1, column=1, sticky="ew", padx=(2, 0))

# --- Sidebar Section 5: Calibration & Defaults ---
cal_frame = tk.LabelFrame(panel, text="CALIBRATION & DEFAULTS", font=("Arial", 8, "bold"), bg="#f5f5f5", padx=4, pady=2)
cal_frame.pack(fill=tk.X, pady=2)

cal_row = tk.Frame(cal_frame, bg="#f5f5f5")
cal_row.pack(fill=tk.X, pady=(0, 2))
tk.Label(cal_row, text="Pixel Pitch (µm):", font=("Arial", 7), bg="#f5f5f5").pack(side=tk.LEFT)
pitch_entry = tk.Entry(cal_row, width=6, font=("Arial", 8))
pitch_entry.insert(0, str(CURRENT_SETTINGS.get("pixel_pitch_um", 35.0)))
pitch_entry.pack(side=tk.RIGHT)

tk.Button(cal_frame, text="Save Current Settings as Default", font=("Arial", 8, "bold"), bg="#d9e1f2", command=save_current_as_defaults).pack(fill=tk.X, pady=1)
tk.Button(cal_frame, text="Select Scanner Manually...", font=("Arial", 7), bg="#e0e0e0", command=lambda: scan_plate(force_dialog=True)).pack(fill=tk.X, pady=1)

# Bottom Status Bar
status_bar = tk.Frame(root, relief=tk.SUNKEN, bd=1, bg="#e8e8e8", padx=8, pady=3)
status_bar.pack(side=tk.BOTTOM, fill=tk.X)

status_lbl = tk.Label(status_bar, text="Ready", bg="#e8e8e8", anchor="w")
status_lbl.pack(side=tk.LEFT, padx=5)

cursor_info_lbl = tk.Label(status_bar, text="Pixel: (0, 0) | Raw 16-bit: 0", bg="#e8e8e8", fg="#333333")
cursor_info_lbl.pack(side=tk.LEFT, padx=25)

meas_info_lbl = tk.Label(status_bar, text="Caliper: None", bg="#e8e8e8", font=("Arial", 9, "bold"), fg="#0044cc")
meas_info_lbl.pack(side=tk.RIGHT, padx=10)

# -------------------------------------------------------------
# 8. Dynamic Auto-Sizing with Extra Height Buffer
# -------------------------------------------------------------
root.update_idletasks()

req_panel_h = panel.winfo_reqheight()
req_tool_h = toolbar.winfo_reqheight()
req_stat_h = status_bar.winfo_reqheight()
# Extra 35px buffer ensures scrollbar is never needed on startup
total_needed_h = req_panel_h + req_tool_h + req_stat_h + 35

screen_w = root.winfo_screenwidth()
screen_h = root.winfo_screenheight()

target_h = min(total_needed_h, screen_h - 70)
target_w = min(1300, screen_w - 40)
pos_x = max(0, (screen_w - target_w) // 2)
pos_y = max(0, (screen_h - target_h - 40) // 2)

root.geometry(f"{target_w}x{target_h}+{pos_x}+{pos_y}")
root.minsize(980, 640)

root.mainloop()