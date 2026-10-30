import os
import sys
import json
import time
import struct
import tempfile
import datetime
import subprocess
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
    "palette": "X-Ray Teardown (Jet)",
    "brightness": 0.0,
    "contrast": 1.2,
    "gamma": 1.1,
    "sharpness": 1.0,
    "clahe_enabled": True,
    "clahe_clip": 2.0,
    "invert": True,
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

# Ensure TWAIN support DLL paths are registered
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

zoom_scale = 1.0
offset_x = 0.0
offset_y = 0.0
pan_start_x = 0
pan_start_y = 0
is_panning = False

measuring = False
meas_start_img = None
meas_current_img = None

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
# 3. TWAIN Driver Selection & Ingestion
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

def process_image_pipeline(raw_img):
    global raw_min, raw_max
    if raw_img is None:
        return None

    brightness = brightness_slider.get()
    contrast = contrast_slider.get()
    gamma = gamma_slider.get()
    invert = invert_var.get()
    sharpness = sharp_slider.get()
    clahe_en = clahe_var.get()
    clahe_clip = clahe_slider.get()
    cmap_choice = cmap_var.get()

    x = np.arange(65536, dtype=np.float32)
    denom = max(raw_max - raw_min, 1.0)
    norm = np.clip((x - raw_min) / denom + brightness, 0.0, 1.0)

    if contrast != 1.0:
        norm = np.clip((norm - 0.5) * contrast + 0.5, 0.0, 1.0)
    if gamma != 1.0:
        norm = np.power(norm, 1.0 / max(gamma, 0.01))
    if invert:
        norm = 1.0 - norm

    lut_8 = np.clip(norm * 255.0 + 0.5, 0, 255).astype(np.uint8)
    base_8 = lut_8[raw_img]

    if clahe_en:
        clahe = cv2.createCLAHE(clipLimit=max(clahe_clip, 0.1), tileGridSize=(8, 8))
        base_8 = clahe.apply(base_8)

    if sharpness > 0:
        blurred = cv2.GaussianBlur(base_8, (0, 0), sigmaX=sharpness)
        base_8 = cv2.addWeighted(base_8, 1.0 + (sharpness * 0.7), blurred, -(sharpness * 0.7), 0)

    cmap = COLORMAP_MODES.get(cmap_choice)
    if cmap is not None:
        color_bgr = cv2.applyColorMap(base_8, cmap)
        return cv2.cvtColor(color_bgr, cv2.COLOR_BGR2RGB)
    return base_8

def update_processed_image(*args):
    global processed_full_image
    if current_raw_16bit is None:
        return
    processed_full_image = process_image_pipeline(current_raw_16bit)
    redraw_canvas()

# -------------------------------------------------------------
# 4. Canvas Viewport, Zoom, Pan & Caliper Drawing
# -------------------------------------------------------------
def redraw_canvas():
    global processed_full_image, zoom_scale, offset_x, offset_y
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
    if processed_full_image is None:
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
    if current_raw_16bit is None:
        return
    ix, iy = canvas_to_img_coords(event.x, event.y)
    raw_val = current_raw_16bit[iy, ix]
    cursor_info_lbl.config(text=f"Pixel: ({ix}, {iy}) | Raw 16-bit: {raw_val}")

def on_left_press(event):
    global pan_start_x, pan_start_y, is_panning, measuring, meas_start_img, meas_current_img
    if caliper_tool_var.get():
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
    if caliper_tool_var.get() and measuring:
        meas_current_img = canvas_to_img_coords(event.x, event.y)
        redraw_canvas()
    elif is_panning:
        offset_x = event.x - pan_start_x
        offset_y = event.y - pan_start_y
        redraw_canvas()

def on_left_release(event):
    global is_panning, measuring
    is_panning = False
    if measuring:
        measuring = False
        redraw_canvas()

def on_right_press(event):
    global measuring, meas_start_img, meas_current_img
    measuring = True
    meas_start_img = canvas_to_img_coords(event.x, event.y)
    meas_current_img = meas_start_img
    redraw_canvas()

def on_right_drag(event):
    global meas_current_img
    if measuring:
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
# 5. Acquisition Workflow
# -------------------------------------------------------------
def scan_plate(force_dialog=False):
    global current_raw_16bit, raw_min, raw_max
    sm = None
    sd = None

    try:
        status_lbl.config(text="Connecting to FireCR...")
        root.update()

        hwnd = root.winfo_id()
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

        # Modal acquisition dialog
        sd.request_acquire(show_ui=True, modal_ui=True)
        sd.modal_loop()

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

        h, w = current_raw_16bit.shape[:2]
        status_lbl.config(text=f"Loaded: 16-bit ({w}x{h}) | Range: [{int(raw_min)} - {int(raw_max)}]")
        update_processed_image()
        fit_to_window()

    except Exception as e:
        status_lbl.config(text="Ready")
    finally:
        if sd:
            try: sd.destroy()
            except Exception: pass
        if sm:
            try: sm.destroy()
            except Exception: pass

def rotate_plate():
    global current_raw_16bit
    if current_raw_16bit is None:
        return
    current_raw_16bit = cv2.rotate(current_raw_16bit, cv2.ROTATE_90_CLOCKWISE)
    update_processed_image()
    fit_to_window()

def save_image():
    global last_saved_path
    if current_raw_16bit is None or processed_full_image is None:
        messagebox.showwarning("Warning", "Scan a plate first.")
        return

    # DDMMYY-HHMMSS.tiff format
    now = datetime.datetime.now()
    default_name = datetime.datetime.now().strftime("Scan_%Y%m%d_%H%M%S.tif")

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
                brightness = brightness_slider.get()
                contrast = contrast_slider.get()
                gamma = gamma_slider.get()
                invert = invert_var.get()

                x = np.arange(65536, dtype=np.float32)
                denom = max(raw_max - raw_min, 1.0)
                norm = np.clip((x - raw_min) / denom + brightness, 0.0, 1.0)
                if contrast != 1.0:
                    norm = np.clip((norm - 0.5) * contrast + 0.5, 0.0, 1.0)
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
    CURRENT_SETTINGS["brightness"] = brightness_slider.get()
    CURRENT_SETTINGS["contrast"] = contrast_slider.get()
    CURRENT_SETTINGS["gamma"] = gamma_slider.get()
    CURRENT_SETTINGS["sharpness"] = sharp_slider.get()
    CURRENT_SETTINGS["clahe_enabled"] = clahe_var.get()
    CURRENT_SETTINGS["clahe_clip"] = clahe_slider.get()
    CURRENT_SETTINGS["invert"] = invert_var.get()
    try:
        CURRENT_SETTINGS["pixel_pitch_um"] = float(pitch_entry.get())
    except ValueError:
        pass
    save_settings(CURRENT_SETTINGS)
    messagebox.showinfo("Settings", f"Settings saved to:\n{SETTINGS_FILE}")

def reset_all_controls():
    brightness_slider.set(0.0)
    contrast_slider.set(1.0)
    gamma_slider.set(1.0)
    sharp_slider.set(0.0)
    clahe_var.set(False)
    clahe_slider.set(2.0)
    invert_var.set(False)
    cmap_var.set("Monochrome (Raw)")
    update_processed_image()

# -------------------------------------------------------------
# 6. GUI Layout Construction
# -------------------------------------------------------------
root = tk.Tk()
root.title("FireCR 16-bit Radiography & Electronics Inspection Suite")
root.geometry("1100x780")
root.minsize(900, 650)

# Top Action Toolbar
toolbar = tk.Frame(root, bg="#2a2a2a", pady=6)
toolbar.pack(fill=tk.X)

tk.Button(toolbar, text="Acquire from FireCR", font=("Arial", 10, "bold"), bg="#2b579a", fg="white", padx=12, pady=3, command=lambda: scan_plate(force_dialog=False)).pack(side=tk.LEFT, padx=5)
tk.Button(toolbar, text="Rotate 90°", font=("Arial", 9, "bold"), bg="#4a4a4a", fg="white", padx=8, pady=3, command=rotate_plate).pack(side=tk.LEFT, padx=3)
tk.Button(toolbar, text="Fit View", font=("Arial", 9), bg="#4a4a4a", fg="white", padx=8, pady=3, command=fit_to_window).pack(side=tk.LEFT, padx=3)
tk.Button(toolbar, text="1:1 Native Pixels", font=("Arial", 9), bg="#4a4a4a", fg="white", padx=8, pady=3, command=zoom_1to1).pack(side=tk.LEFT, padx=3)

caliper_tool_var = tk.BooleanVar(value=False)
tk.Checkbutton(toolbar, text="Caliper Tool", variable=caliper_tool_var, font=("Arial", 9, "bold"), bg="#ffaa00", selectcolor="#2a2a2a", indicatoron=False, padx=8, pady=3).pack(side=tk.LEFT, padx=6)
tk.Button(toolbar, text="Clear Measure", font=("Arial", 8), bg="#3d3d3d", fg="white", padx=6, pady=3, command=clear_caliper).pack(side=tk.LEFT, padx=3)

tk.Button(toolbar, text="Open Folder", font=("Arial", 9), bg="#3d3d3d", fg="white", padx=8, pady=3, command=open_save_folder).pack(side=tk.RIGHT, padx=5)
tk.Button(toolbar, text="Save Image...", font=("Arial", 10, "bold"), bg="#107c41", fg="white", padx=12, pady=3, command=save_image).pack(side=tk.RIGHT, padx=5)

# Main Workspace
main_frame = tk.Frame(root)
main_frame.pack(fill=tk.BOTH, expand=True)

canvas_frame = tk.Frame(main_frame, bg="#121212")
canvas_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

canvas = tk.Canvas(canvas_frame, bg="#121212", highlightthickness=0)
canvas.pack(fill=tk.BOTH, expand=True)

canvas.bind("<Configure>", lambda e: redraw_canvas())
canvas.bind("<Motion>", on_mouse_move)
canvas.bind("<ButtonPress-1>", on_left_press)
canvas.bind("<B1-Motion>", on_left_drag)
canvas.bind("<ButtonRelease-1>", on_left_release)
canvas.bind("<ButtonPress-3>", on_right_press)
canvas.bind("<B3-Motion>", on_right_drag)
canvas.bind("<ButtonRelease-3>", on_right_release)
canvas.bind("<MouseWheel>", on_mouse_wheel)
canvas.bind("<Button-4>", on_mouse_wheel)
canvas.bind("<Button-5>", on_mouse_wheel)

panel = tk.Frame(main_frame, width=280, padx=12, pady=8, bg="#f5f5f5")
panel.pack(side=tk.RIGHT, fill=tk.Y)
panel.pack_propagate(False)

tk.Label(panel, text="INSPECTION CONTROLS", font=("Arial", 10, "bold"), bg="#f5f5f5").pack(anchor="w", pady=(0, 6))

tk.Label(panel, text="Pseudo-Color Palette:", font=("Arial", 8, "bold"), bg="#f5f5f5").pack(anchor="w")
cmap_var = tk.StringVar(value=CURRENT_SETTINGS.get("palette", "X-Ray Teardown (Jet)"))
cmap_dropdown = ttk.Combobox(panel, textvariable=cmap_var, values=list(COLORMAP_MODES.keys()), state="readonly")
cmap_dropdown.pack(fill=tk.X, pady=(0, 4))
cmap_dropdown.bind("<<ComboboxSelected>>", update_processed_image)

invert_var = tk.BooleanVar(value=CURRENT_SETTINGS.get("invert", True))
tk.Checkbutton(panel, text="Invert Density (Bone/Metal White)", variable=invert_var, bg="#f5f5f5", command=update_processed_image).pack(anchor="w", pady=(0, 6))

clahe_frame = tk.LabelFrame(panel, text="CLAHE (Adaptive Local Contrast)", font=("Arial", 8, "bold"), bg="#f5f5f5", padx=6, pady=4)
clahe_frame.pack(fill=tk.X, pady=4)
clahe_var = tk.BooleanVar(value=CURRENT_SETTINGS.get("clahe_enabled", True))
tk.Checkbutton(clahe_frame, text="Enable Local Equalization", variable=clahe_var, bg="#f5f5f5", command=update_processed_image).pack(anchor="w")
tk.Label(clahe_frame, text="Clip Limit (Strength):", font=("Arial", 7), bg="#f5f5f5").pack(anchor="w")
clahe_slider = tk.Scale(clahe_frame, from_=0.5, to=6.0, resolution=0.1, orient=tk.HORIZONTAL, bg="#f5f5f5", command=update_processed_image)
clahe_slider.set(CURRENT_SETTINGS.get("clahe_clip", 2.0))
clahe_slider.pack(fill=tk.X)

tk.Label(panel, text="Brightness / Window Level:", font=("Arial", 8, "bold"), bg="#f5f5f5").pack(anchor="w", pady=(4, 0))
brightness_slider = tk.Scale(panel, from_=-0.5, to=0.5, resolution=0.01, orient=tk.HORIZONTAL, bg="#f5f5f5", command=update_processed_image)
brightness_slider.set(CURRENT_SETTINGS.get("brightness", 0.0))
brightness_slider.pack(fill=tk.X)

tk.Label(panel, text="Contrast:", font=("Arial", 8, "bold"), bg="#f5f5f5").pack(anchor="w")
contrast_slider = tk.Scale(panel, from_=0.2, to=3.0, resolution=0.05, orient=tk.HORIZONTAL, bg="#f5f5f5", command=update_processed_image)
contrast_slider.set(CURRENT_SETTINGS.get("contrast", 1.2))
contrast_slider.pack(fill=tk.X)

tk.Label(panel, text="Gamma Curve:", font=("Arial", 8, "bold"), bg="#f5f5f5").pack(anchor="w")
gamma_slider = tk.Scale(panel, from_=0.2, to=3.0, resolution=0.05, orient=tk.HORIZONTAL, bg="#f5f5f5", command=update_processed_image)
gamma_slider.set(CURRENT_SETTINGS.get("gamma", 1.1))
gamma_slider.pack(fill=tk.X)

tk.Label(panel, text="Edge Sharpness (Unsharp Mask):", font=("Arial", 8, "bold"), bg="#f5f5f5").pack(anchor="w")
sharp_slider = tk.Scale(panel, from_=0.0, to=3.0, resolution=0.1, orient=tk.HORIZONTAL, bg="#f5f5f5", command=update_processed_image)
sharp_slider.set(CURRENT_SETTINGS.get("sharpness", 1.0))
sharp_slider.pack(fill=tk.X)

cal_frame = tk.LabelFrame(panel, text="Caliper Calibration", font=("Arial", 8, "bold"), bg="#f5f5f5", padx=6, pady=4)
cal_frame.pack(fill=tk.X, pady=6)
cal_row = tk.Frame(cal_frame, bg="#f5f5f5")
cal_row.pack(fill=tk.X)
tk.Label(cal_row, text="Pixel Pitch (µm):", font=("Arial", 8), bg="#f5f5f5").pack(side=tk.LEFT)
pitch_entry = tk.Entry(cal_row, width=6)
pitch_entry.insert(0, str(CURRENT_SETTINGS.get("pixel_pitch_um", 35.0)))
pitch_entry.pack(side=tk.RIGHT)

tk.Button(panel, text="Save Settings as Default", font=("Arial", 8, "bold"), bg="#d9e1f2", command=save_current_as_defaults).pack(fill=tk.X, pady=(8, 2))
tk.Button(panel, text="Select Scanner Manually...", font=("Arial", 8), bg="#e0e0e0", command=lambda: scan_plate(force_dialog=True)).pack(fill=tk.X, pady=2)
tk.Button(panel, text="Reset Sliders", font=("Arial", 8), bg="#e0e0e0", command=reset_all_controls).pack(fill=tk.X, pady=2)

status_bar = tk.Frame(root, relief=tk.SUNKEN, bd=1, bg="#e8e8e8", padx=8, pady=3)
status_bar.pack(side=tk.BOTTOM, fill=tk.X)

status_lbl = tk.Label(status_bar, text="Ready", bg="#e8e8e8", anchor="w")
status_lbl.pack(side=tk.LEFT, padx=5)

cursor_info_lbl = tk.Label(status_bar, text="Pixel: (0, 0) | Raw 16-bit: 0", bg="#e8e8e8", fg="#333333")
cursor_info_lbl.pack(side=tk.LEFT, padx=25)

meas_info_lbl = tk.Label(status_bar, text="Caliper: None", bg="#e8e8e8", font=("Arial", 9, "bold"), fg="#0044cc")
meas_info_lbl.pack(side=tk.RIGHT, padx=10)

root.mainloop()