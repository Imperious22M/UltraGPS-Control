# Conversation Summary - UltraGPS Control Application

## Date: 2026-01-29

## Latest Changes: Keyboard Focus Fixes and Compass Rose Redrawing

### Fixed Keyboard Focus Loss Issues
Multiple issues caused keyboard focus to be lost when changing windows, opening dialogs, or resizing the arena. Implemented several layers of fixes:

**1. Canvas Focus Prevention:**
- Set `takefocus=False` on all `FigureCanvasTkAgg` canvas widgets
- Prevents matplotlib canvas from stealing keyboard focus from entry widgets

**2. Non-blocking Canvas Draws:**
- Changed `canvas.draw()` to `canvas.draw_idle()` in ArenaMakerWindow update methods
- Less aggressive redraw that doesn't steal focus

**3. Animation Loop Focus Preservation:**
- Modified `_refresh_animations()` to save/restore focus around canvas draws
- Saves current focused widget before drawing, restores after

**4. Modal Dialog Animation Pausing:**
- Added `_pause_process_queue()` at start of `_ask_arena_dimensions()` and `_show_verify_result()`
- Added `_resume_process_queue()` after dialog closes
- Prevents animation loop from stealing focus while dialogs are open
- Added `dialog.focus_force()` after `grab_set()` to ensure dialog has focus

### Added Restore Keyboard Button
- Added "Restore Keyboard" button to ArenaMaker window top bar
- Button appears next to the title
- Calls `self.root.focus_force()` to restore keyboard focus as a fallback workaround

### Compass Rose Redrawing on Arena Updates
All window classes now redraw the X/Y compass rose when receiver coordinates are updated:

**Added to PositionWindow and CalibrationWindow:**
- `self.compass_x_arrow`, `self.compass_x_text` - X-axis compass elements
- `self.compass_y_arrow`, `self.compass_y_text` - Y-axis compass elements
- `_draw_compass_rose()` method - Removes existing elements and redraws based on current axis limits

**Changes:**
- `__init__()` now calls `_draw_compass_rose()` instead of inline drawing
- `update_arena()` now calls `_draw_compass_rose()` after updating axis limits
- Compass rose arrow length scales to 8% of the smaller axis dimension
- ArenaMakerWindow already had this functionality

---

## Previous Changes: Removed Extraneous grid_width and grid_height

### Cleaned Up All Window Classes
Removed all extraneous `grid_width` and `grid_height` parameters and attributes that were no longer used since axis limits are calculated from actual receiver positions:

**Changes to __init__() methods:**
- Removed `grid_width` and `grid_height` parameters from `PositionWindow.__init__()`, `CalibrationWindow.__init__()`, and `ArenaMakerWindow.__init__()`
- Removed `self.grid_width` and `self.grid_height` attribute assignments
- Now calculate axis limits from receiver positions in `__init__()` instead

**Changes to update_arena() methods:**
- Removed `grid_width` and `grid_height` parameters from all `update_arena()` methods
- Simplified to only take `receiver_positions` as parameter

**Changes to update_receiver_positions():**
- Removed `width` and `height` parameters from `ArenaMakerWindow.update_receiver_positions()`

**Changes to _calculate_positions_from_distances():**
- Now returns just `positions` list instead of `(positions, width, height)` tuple

**Changes to window instantiation:**
```python
# Before:
self.position_window = PositionWindow(
    grid_width=self.settings_module.arena_size[0],
    grid_height=self.settings_module.arena_size[1],
    receiver_positions=self.settings_module.get_tower_coordinates()
)

# After:
self.position_window = PositionWindow(
    receiver_positions=self.settings_module.get_tower_coordinates()
)
```

**Changes to update_arena calls:**
```python
# Before:
receiver_positions = self.settings_module.get_tower_coordinates()
grid_width, grid_height = self.settings_module.arena_size
self.position_window.update_arena(grid_width, grid_height, receiver_positions)

# After:
receiver_positions = self.settings_module.get_tower_coordinates()
self.position_window.update_arena(receiver_positions)
```

---

## Previous Changes: Bounds-Based Arena Resizing for All Windows

### Updated update_arena() Methods
All window classes now calculate axis limits from actual receiver positions (same as ArenaMakerWindow):

**PositionWindow.update_arena() and CalibrationWindow.update_arena():**
```python
# Calculate bounds from receiver positions
receiver_x = [pos[1][0] for pos in receiver_positions]
receiver_y = [pos[1][1] for pos in receiver_positions]
min_x, max_x = min(receiver_x), max(receiver_x)
min_y, max_y = min(receiver_y), max(receiver_y)

# Update axis limits based on actual receiver positions with padding
self.ax.set_xlim(min_x - self.grid_padding, max_x + self.grid_padding)
self.ax.set_ylim(min_y - self.grid_padding, max_y + self.grid_padding)
```

**Result:**
- When any receiver position is changed, the window axis limits resize to fit actual receiver bounds
- All windows (Position, Calibration, ArenaMaker) now have consistent resizing behavior

---

## Previous Changes: Arena Size Property Auto-Recalculation

### SettingsModule.arena_size Property
Converted `arena_size` from an instance attribute to a property that always recalculates from receiver positions:

```python
@property
def arena_size(self):
    """Get the arena size (width, height) calculated from receiver positions."""
    return self._calculate_arena_size()
```

**Changes:**
- Removed `self.arena_size = ...` assignments from `__init__`, `reload_config()`, and `valid_settings.setter`
- Added `@property` that always calls `_calculate_arena_size()`
- Every access to `arena_size` now recalculates from current receiver positions

**Calculation Method (`_calculate_arena_size`):**
- Width: max of |receiver 3 - receiver 6| x-distance and |receiver 1 - receiver 4| x-distance
- Height: max of |receiver 3 - receiver 1| y-distance and |receiver 6 - receiver 4| y-distance

---

## Previous Changes: Arena Update Methods for All Windows

### Added update_arena() Methods
Added `update_arena(grid_width, grid_height, receiver_positions)` method to all window classes:

1. **PositionWindow.update_arena()**
   - Updates grid dimensions and receiver positions
   - Calculates axis limits from actual receiver bounds
   - Redraws receiver scatter, labels, and connection lines
   - Reinitializes CEP positioning with new receiver positions
   - Clears position history

2. **CalibrationWindow.update_arena()**
   - Updates grid dimensions and receiver positions
   - Updates axis limits
   - Redraws receiver scatter, labels, and connection lines
   - Clears position history

3. **ArenaMakerWindow.update_arena()**
   - Wrapper around `update_receiver_positions()` for API consistency

### Updated show_*_window Functions
All show functions now read settings from the settings file and update the window:

1. **show_position_window()**
   - Calls `position_window.update_arena()` with settings from settings file
   - Called after threads are stopped, before showing the frame

2. **show_calibration_window()**
   - Calls `calibration_window.update_arena()` with settings from settings file
   - Called after threads are stopped, before showing the frame

3. **show_arena_maker_window()**
   - Calls `arena_maker_window.update_arena()` when settings are valid
   - Uses settings from settings file

---

## Previous Changes: Re-dimension Button and UI Mode Transformation

### Added Re-dimension Button During Normal Operation
- Added secondary "Re-dimension" button that appears below "Back" button during normal operation
- Button allows re-dimensioning the arena even when settings are already valid
- Button uses same `_redimension_arena` function as initial setup

### UI Mode Transformation on Verify Success
When "Verify Settings" is clicked and verification succeeds during initial setup:
1. Top button changes from "Re-dimension Arena" (cyan) to "← Back" (green)
2. Secondary "Re-dimension" button appears below the Back button
3. Back button returns to main menu
4. UI now matches normal operation mode

### Button Layout
**Initial Setup Mode (invalid settings):**
- Top bar: "Re-dimension Arena" button only (cyan)

**Normal Operation Mode (valid settings):**
- Top bar: "← Back" button (green) + "Re-dimension" button (cyan)

### Storage Added to GraphicsModule
- `self._arena_maker_redim_button` - Reference to secondary re-dimension button

---

## Previous Changes: Verify Settings Button, RC Controls, and Bug Fixes

### Bug Fix: Calibration Points Loading
- Fixed calibration points not displaying when Setup button is pressed with valid settings
- Added call to `update_calibration_points()` during first-time arena maker frame creation

### Added RC_1 and RC_2 Entry Controls
- RC_1 and RC_2 entry fields added next to Cal 1/Cal 2 coordinates
- RC values represent distance from origin to each calibration point
- Changing RC value scales both X and Y coordinates proportionally (maintains angle from origin)
- All entry fields (X, Y, RC) stay synchronized

### Added Verify Settings Button
- Gold "Verify Settings" button added to calibration points control panel
- Saves all displayed values to settings module
- Calls `verify_settings()` in SettingsModule to validate:
  - All 6 receivers exist
  - All receiver positions are non-zero
  - All calibration distances are set
  - All offset parameters exist
  - Both calibration points are set (not both zeros)
- Shows dialog with success message or list of errors
- Sets `valid_settings = true` if verification passes
- Transforms UI to normal operation mode on success

### Methods in GraphicsModule

1. **`_update_rc_from_entries()`**
   - Called when RC_1 or RC_2 entries change
   - Scales both X and Y to maintain angle while changing distance
   - Updates X,Y entry fields to reflect changes
   - Recalculates calibration distances to all receivers

2. **`_verify_and_save_settings()`**
   - Saves all displayed values to settings module
   - Recalculates calibration distances
   - Calls `verify_settings()` and shows result dialog
   - Sets `valid_settings = true` on success
   - Transforms UI to normal operation mode

3. **`_show_verify_result(success, messages)`**
   - Displays verification result dialog
   - Green title for success, red for failure
   - Shows list of messages/errors

### Updated SettingsModule.verify_settings()
- Added check for calibration points (cal_point_1 and cal_point_2 must not both be zeros)

---

## Previous Changes: Calibration Points with Coordinate Controls and RC Lines

### Added Calibration Points to Arena Maker

**Calibration Point Fields in config.toml:**
- `cal_point_1` and `cal_point_2` - [x, y] coordinates below `units` field
- When file created: initialized to `[0.0, 0.0]`
- When arena dimensioned:
  - `cal_point_1 = [0.0, height/4]` (quarter height from origin on Y-axis)
  - `cal_point_2 = [0.0, -height/4]` (negative quarter height from origin)

**Calibration Distance Calculation:**
- When arena is dimensioned, distances from each calibration point to each receiver are calculated
- When R distances (R1-R10) change, calibration distances are recalculated
- When calibration point coordinates change, calibration distances are recalculated
- For each receiver:
  - `cal_distances[0]` = Euclidean distance from cal_point_1 to receiver
  - `cal_distances[1]` = Euclidean distance from cal_point_2 to receiver

**Visual Display:**
- Calibration points shown as green diamonds on the arena plot
- Labels show "Cal 1" and "Cal 2" with coordinates
- Points added to legend as "Cal Points"
- **RC_1**: Green dashed line from origin (0,0) to cal_point_1 with distance label
- **RC_2**: Green dashed line from origin (0,0) to cal_point_2 with distance label

**Calibration Point Coordinate Controls:**
- Control panel below R1-R10 distance controls
- X and Y coordinate entry fields for Cal 1 and Cal 2
- RC_1 and RC_2 distance entry fields
- "Verify Settings" button
- Entry fields update calibration points when Enter or focus-out

### Storage Added to ArenaMakerWindow
- `self.cal_point_1_scatter` - Scatter plot for cal point 1
- `self.cal_point_1_label` - Label for cal point 1
- `self.cal_point_2_scatter` - Scatter plot for cal point 2
- `self.cal_point_2_label` - Label for cal point 2
- `self.rc1_line` - RC_1 dashed line from origin to cal_point_1
- `self.rc1_label` - RC_1 distance label
- `self.rc2_line` - RC_2 dashed line from origin to cal_point_2
- `self.rc2_label` - RC_2 distance label

---

## Previous Changes: Receiver Distance Controls

### Added Receiver Distance Input Feature
Added the ability to change distances between receivers in the Arena Maker window:

**Distance Definitions (counterclockwise from receiver 3):**
- **R1**: Distance between receiver 3 and receiver 2 (left side, top to middle)
- **R2**: Distance between receiver 2 and receiver 1 (left side, middle to bottom)
- **R3**: Distance between receiver 1 and receiver 4 (bottom, left to right)
- **R4**: Distance between receiver 4 and receiver 5 (right side, bottom to middle)
- **R5**: Distance between receiver 5 and receiver 6 (right side, middle to top)
- **R6**: Distance between receiver 6 and receiver 3 (top, right to left)
- **R7**: X-distance from origin to receiver 2 (dashed line)
- **R8**: X-distance from origin to receiver 5 (dashed line)
- **R9**: Y-offset of receiver 2 from origin (dashed line, only shown if non-zero)
  - When R9 > 0: R9 is subtracted from R1 to keep receiver 3 in place
  - When R9 < 0: R9 is subtracted from R2 (adds |R9|) to keep receiver 1 in place
  - When R9 returns to 0: previous adjustment is restored (adds prev_R9 back to R1 or R2)
- **R10**: Y-offset of receiver 5 from origin (dashed line, only shown if non-zero)
  - When R10 > 0: R10 is subtracted from R5 to keep receiver 6 in place
  - When R10 < 0: R10 is subtracted from R4 (adds |R10|) to keep receiver 4 in place
  - When R10 returns to 0: previous adjustment is restored (adds prev_R10 back to R4 or R5)

**Layout Diagram:**
```
  3 ----R6---- 6
  |            |
  R1          R5
  |            |
  2 ---R7--(0,0)--R8--- 5
  |            |
  R2          R4
  |            |
  1 ----R3---- 4
```

### New Methods Added to GraphicsModule

1. **`_calculate_positions_from_distances(r1, r2, r3, r4, r5, r6, r7=None, r8=None, r9=0, r10=0)`**
   - Calculates receiver positions from inter-receiver distances
   - R7/R8: X-distances from origin to receivers 2/5 (defaults to R6/2)
   - R9/R10: Y-offsets of receivers 2/5 from origin (defaults to 0)
   - Centers the arena at origin
   - Returns: (positions, width, height)

2. **`_calculate_distances_from_positions(positions)`**
   - Calculates inter-receiver distances from positions
   - Returns: (r1, r2, r3, r4, r5, r6, r7, r8, r9, r10)

3. **`_update_arena_from_distances()`**
   - Called when distance entry fields change
   - Reads values from entry fields, validates, calculates new positions
   - Updates arena display and settings module

### UI Changes to Arena Maker Window

- Added distance control panel below the matplotlib canvas
- 10 entry fields (R1-R10) with labels showing receiver pairs
- R7/R8 have dashed lines (orange) for origin-to-receiver X-distances
- R9/R10 have dashed lines (green) for Y-offsets, only shown when non-zero
- Entry fields update on Enter key or focus-out (no Apply button needed)
- Distance entries auto-update when:
  - Returning to Arena Maker window
  - After using "Re-dimension Arena" dialog
- Arena auto-resizes to fit actual receiver bounds when any R value changes

### Storage Added
- `self._distance_entries = {}` - Dictionary storing entry field references

---

## Previous Completed Tasks

### 1. Created MainWindow Class
Added `MainWindow` class at the end of GraphicsModule.py:
- `__init__()` - Initializes attributes (frame, labels, buttons) and callback placeholders
- `build(parent)` - Creates UI elements inside the parent frame
- Button callback handlers
- `close()` - Cleanup method

### 2. Updated GraphicsModule.__init__
- Added `self.main_window = MainWindow()` instantiation
- Added `self._distance_entries = {}` for distance controls

### 3. Updated show_main_window()
- Uses MainWindow class for UI
- Redirects to Arena Maker if settings invalid

### 4. Updated main.py
- Changed startup from `show_position_window()` to `show_main_window()`
- MainWindow is now the first window that opens

### 5. Previous Session Work
- Added `_main_frame` to cached frames
- Updated `_hide_all_frames()` to include main frame
- Updated all back buttons to navigate to MainWindow
- Added back button to Position window

## Files Modified
- `/home/imp/UltraGPS-Python/Control/GraphicsModule.py`
  - Added MainWindow class
  - Added distance calculation methods
  - Added distance control panel in Arena Maker
- `/home/imp/UltraGPS-Python/Control/main.py`
  - Changed startup window to MainWindow

## Application Flow
```
main.py
  └── GraphicsModule()
        └── start_tk_window()
        └── show_main_window()
              ├── If settings invalid → show_arena_maker_window()
              │     └── Arena dimensions dialog
              │     └── Distance controls (R1-R10)
              └── If settings valid → Main Menu
                    ├── Position button → show_position_window()
                    ├── Calibration button → show_calibration_window()
                    └── Setup button → show_arena_maker_window()
```

## Window Classes in GraphicsModule.py
1. **MainWindow** - Main menu (tkinter only, no matplotlib)
2. **PositionWindow** - Real-time position tracking with matplotlib plots
3. **CalibrationWindow** - Calibration view with X/Y position plot
4. **ArenaMakerWindow** - Arena setup with receiver coordinates display and distance controls
