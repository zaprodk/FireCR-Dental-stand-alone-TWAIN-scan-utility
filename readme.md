# FireCR 16-bit Radiography & Electronics Inspection (V5.0)

A standalone, high-dynamic-range acquisition and analysis desktop application tailored for **FireCR Dental** computed radiography (CR) scanners. Engineered specifically for non-destructive testing (NDT), electronics failure analysis, silicon die inspection, PCB reverse engineering, and radiographic measurement.

![FireCR Inspection Interface](screenshot.png)

---

## Overview

Clinical dental packages bundled with computed radiography hardware often lock scans into closed patient databases, truncate data to 8-bit displays, and apply unalterable medical tone curves. 

This utility connects directly to the 32-bit TWAIN driver (`FireCRDentTwain32.ds`) to extract **true, uncompressed 16-bit linear sensor data** straight from memory buffers. Version 5.0 introduces comprehensive radiographic digital signal processing—including true Window Width/Leveling with live on-canvas mouse drag, real-time log-scaled histogram feedback, latitude dynamic range compression, dual-axis edge enhancement, and calibrated metric calipers.

---

## What's New in V5.0

* **Radiographic Windowing (WW / WL):** Calibrated 16-bit Window Center (Level) and Window Width controls replace generic photo sliders, matching medical/NDT standards.
* **Interactive On-Image Mouse Dragging:** Drag anywhere on the canvas with the **Middle Mouse Button** (or **Ctrl + Left Click**) to adjust windowing in real time:
  * **Vertical Drag:** Shifts Window Center (Level / tissue density penetration).
  * **Horizontal Drag:** Expands or narrows Window Width (contrast steepness).
* **Live 16-bit Log Intensity Histogram:** Real-time detector histogram overlay showing active display window boundaries and center lines directly over the sensor's dynamic distribution.
* **Latitude Reduction (Dynamic Range Compression):** Spatial unsharp low-frequency compression that suppresses extreme density gradients (such as ground planes and lead frames) so fine $25\ \mu\text{m}$ bonding wires and dense metal components remain visible simultaneously.
* **Dual-Axis Edge Filtering:** Independent control over **Edge Gain** (0–10) and **Edge Frequency / Scale** (1–5) to selectively target microscopic wire bonds without blowing out macroscopic package contours into halos.
* **Edge-Preserving Noise Reduction:** Fast bilateral filtering to smooth high-frequency phosphor plate grain and PMT shot noise while keeping edge transitions razor-sharp.
* **One-Click Inspection Presets:** Instant configuration profiles for common electronics inspection workflows (*Linear Raw*, *Wire Bond & Die*, *Solder & Voids*, *BGA Structural*).
* **Python 3.13 Win32 Modal Protection:** Hardened against message-pump GIL re-entrancy crashes during scanner modal loops.
* **Responsive Compact Layout:** Re-engineered sidebar with auto-sizing geometry and auto-hiding scrollbars that fits 1080p displays natively.

---

## Key Features

* **True 16-bit Acquisition:** Direct parsing of native TWAIN DIB memory structures to preserve the full dynamic range of the scanner's photomultiplier tube (PMT).
* **Calibrated Digital Caliper:** On-the-fly component dimensioning, trace clearance, and pitch measurements in millimeters (**mm**), thousandths of an inch (**mil**), and raw **pixels**.
* **Pseudo-Color False-Color Palettes:** Real-time color mapping to highlight density changes:
  * *Monochrome (Raw Grayscale)*
  * *X-Ray Teardown (Jet)*
  * *Turbo (Perceptually Uniform)*
  * *Inferno (Thermal Density)*
  * *Ocean (Clinical Blue)*
  * *Hot (Black to Red/Yellow)*
* **CLAHE (Local Adaptive Equalization):** Localized histogram equalization to pull hidden routing out of dark shadows.
* **Hardware-Accelerated Viewport:** Fast fractional zoom (up to 15×), pan, 90° array rotation, and 1:1 native sensor pixel inspection.
* **Archival Export:** Exports full dynamic range 16-bit TIFF files (`DDMMYY-HHMMSS.tiff`) with current windowing calculations preserved, or 8-bit PNG snapshots.

---

## System Requirements & Architecture

Because the FireCR driver architecture and TWAIN Data Source (`FireCRDentTwain32.ds` / `CRSwing.dll`) are compiled strictly as **32-bit (x86) binaries**, this software **must run under a 32-bit Python environment**. A 64-bit Python runtime cannot bind to 32-bit TWAIN managers (`twain_32.dll`).

* **Operating System:** Windows 10 / 11 (64-bit OS fully supported via 32-bit Python runtime).
* **Hardware:** 3DISC / Digiray / Quantor FireCR Dental scanner.
* **Driver:** FireCR Dental TWAIN Driver installed (`C:\Windows\twain_32\FireCRDental\FireCRDentTwain32.ds`).
* **Python Runtime:** Python 3.10 – 3.13 **(32-bit / x86 version)**.

---

## Installation

### 1. Install 32-bit Python
Download the **Windows installer (32-bit)** from [python.org](https://www.python.org/downloads/windows/). Ensure **"Add python.exe to PATH"** is checked during setup.

Verify your 32-bit interpreter:
```cmd
py -3-32 --version
```

### 2. Clone the Repository
```cmd
git clone https://github.com/your-username/firecr-radiography.git
cd firecr-radiography
```

### 3. Install Dependencies
```cmd
py -3-32 -m pip install numpy opencv-python pillow pytwain
```

---

## Viewport & Mouse Controls

| Input | Action |
| :--- | :--- |
| **Middle-Click + Drag** | **Interactive Window/Level:** Vertical = Center, Horizontal = Width |
| **Ctrl + Left-Click + Drag** | **Interactive Window/Level** (Alternative for laptops/touchpads) |
| **Left-Click + Drag** | Pan image across canvas (when Caliper is off) |
| **Mouse Wheel** | Zoom in / Zoom out centered on mouse cursor |
| **Right-Click + Drag** | Quick Caliper Measurement |
| **Fit View** | Auto-scale image to current viewport |
| **1:1 Native Pixels** | 100% unscaled detector resolution |
| **Rotate 90°** | Rotate detector array 90° clockwise |

---

## Inspection Presets

| Preset | Target Application | Signal Processing Chain |
| :--- | :--- | :--- |
| **Linear (Raw)** | Flat sensor data inspection | Unprocessed full-scale linear window, monochrome palette. |
| **Wire Bond & Die** | Micro-bonding wires, silicon dies | Mid-low window level, high-frequency edge boost (Scale 1), bilateral denoising, CLAHE. |
| **Solder & Voids** | BGA balls, solder bridging, voids | Higher window level, medium edge scale, *Jet* pseudo-color colormap. |
| **BGA Structural** | Large package frames, ground planes | Heavy Latitude Reduction (Level 8) to suppress ground plane blowout, Macro edge enhancement. |

---

## Technical Specifications

* **Row Stride Unpacking:** Bypasses GDI 8-bit truncation using direct 32-bit aligned 16-bit row-stride memory reading:
  $$\text{Row Stride} = \left\lfloor \frac{\text{Width} \times 2 + 3}{4} \right\rfloor \times 4$$
* **Percentile Auto-Ranging:** Computes 0.1% and 99.9% intensity percentiles upon capture to eliminate dead/hot detector pixel bias when setting initial Window Center/Width.
* **Persistent Settings:** Slider positions, custom pixel pitches, palettes, and directory paths automatically save to `settings.txt`.

---

## License

Distributed under the MIT License. See `LICENSE` for details.