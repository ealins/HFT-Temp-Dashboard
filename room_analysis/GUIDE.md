## How Room Analysis works

**In one sentence:** for one room, it uses the CO₂ and temperature sensor readings together with the room's geometry from the IFC model to estimate how well the room is ventilated and how much heat it loses, then lets you compare rooms and try ventilation scenarios.

It is a **simple steady-state model**. It is good for comparing rooms and testing "what if" cases. It is **not** a calibrated simulation and gives no exact energy bill. SimStadt (the building-level simulation) is a separate view.

### 1. What data goes in

| Input | Where it comes from | Notes |
|---|---|---|
| CO₂ (ppm) | CO₂ sensor database | Every CO₂ sensor also records temperature and humidity |
| Indoor temperature | The room's temperature sensor; if the room has none, the **thermometer inside its CO₂ sensor** | The source is shown under the heat-balance numbers |
| Outdoor temperature | DWD station Stuttgart-Echterdingen (`data/outdoor.db`); otherwise Open-Meteo online; otherwise a fixed value you set | Hourly. The airport is on the Filder plateau and is usually a little colder than the campus |
| Room area, volume, height | IFC model, extracted once per building into `envelope.json` | The view never opens the IFC while running |
| External wall, window and door areas, facade direction | IFC geometry (see section 5) | Matched by geometry, not by the IFC "external" flags, which are unreliable |
| **U-values (insulation quality)** | **Assumption** | The IFC only has Revit defaults (e.g. a thin concrete wall at 6.97 W/m²K), so you choose a construction standard and can edit every value |

**Time window.** The window (24 h, 7 days or 31 days) ends at the selected room's last reading, or at the dashboard's end if that is earlier. A room whose sensor stopped earlier therefore still shows its last period instead of an empty chart.

### 2. Tab "Occupancy & ventilation"

- **Mean, peak, hours above the limit.** A reading counts for its typical sampling interval (the median spacing between readings). *Hours above 1000 ppm* = number of readings at or above the limit × that interval.
- **Occupied-hours proxy.** There is no occupancy sensor, so hours with CO₂ ≥ 800 ppm are used as a stand-in for "people were in the room".
- **Air-change rate from CO₂ decay.** After people leave, CO₂ falls towards the outdoor level. For a well-mixed room:

  $$\ln(C(t)-C_{out}) = a - n\,t$$

  where *n* is the air changes per hour. The method:
  1. Smooth the series with a 3-point median.
  2. Find stretches where CO₂ only falls (no rise above 10 ppm between readings, no gap longer than 3.5 sampling intervals).
  3. Keep stretches of at least 30 minutes, with at least 4 points, starting at least 100 ppm above outdoor level. Only points more than 30 ppm above outdoor level are fitted.
  4. Fit a straight line to the logarithm. A falling slope gives *n*.
  5. Keep fits with R² ≥ 0.85 and 0.05 ≤ *n* ≤ 15 per hour. The **median** of the kept fits is the room's air-change rate. The table in the expander shows every episode.
- **Warning for low sensors.** If the 5th percentile of the readings is more than 40 ppm below the outdoor CO₂ value, the sensor probably reads low (calibration drift). Hours above the limit are then under-estimated and the air-change rate is uncertain.

*Assumptions:* the air is well mixed, outdoor CO₂ is the value you set (default 420 ppm), and no CO₂ arrives from neighbouring rooms.

### 3. Tab "Heat balance"

Heat loss is split into **transmission** (through the building envelope) and **ventilation** (air exchange). Each is a conductance in W/K, meaning watts lost per kelvin of indoor-outdoor difference.

$$H_{tr}=\sum_i A_i\,U_i\,b_i \qquad H_{v}=0.34\cdot n\cdot V \qquad H=H_{tr}+H_{v}$$

| Element | Area A | U-value | Reduction factor b |
|---|---|---|---|
| External walls | wall area minus windows and doors | wall | 1 |
| Windows | window area | window | 1 |
| External doors | door area | door | 1 |
| Roof | room floor area, if the room is on the top storey | roof | 1 |
| Floor to ground | room floor area, if on the ground storey | ground | *ground factor* (default 0.6) |
| Atrium side | walls and windows facing an enclosed courtyard | wall / window | *atrium factor* (default 0.5) |
| Ventilation | – | 0.34 Wh/(m³K) × *n* × room volume V | 1 |

The factors *b* reduce the heat flow where the other side is the ground or an unheated atrium instead of outdoor air. Both are adjustable.

**Energy over the window.** For every hour with indoor temperature Tᵢ and outdoor temperature Tₒ:

$$Q=\sum_{hours} H\cdot\max(T_i-T_o,\,0)\ /\ 1000 \quad\text{[kWh]}$$

Hours when it is warmer outside than inside count as zero (no heating, and no cooling is modelled). Neighbouring rooms are assumed to be at the same temperature, so there is no heat flow through interior walls, floors or ceilings.

**Construction presets (assumptions, W/m²K)**

| Standard | Wall | Window | Door | Roof | Ground |
|---|---|---|---|---|---|
| Unrenovated (1960s–70s) | 1.4 | 2.8 | 3.0 | 0.9 | 1.0 |
| Partly retrofitted (1980s–2000s) | 0.8 | 1.8 | 2.2 | 0.5 | 0.8 |
| Modern (EnEV 2014 / GEG) | 0.28 | 1.0 | 1.5 | 0.2 | 0.35 |

### 4. Tab "Compare rooms"

One row per room (this floor, or the whole building, up to 60 rooms) over the same window: mean temperature, share of time above 26 °C and below 20 °C, peak CO₂, hours above 1000 ppm, area, window area, window share of floor area, main facade, roof yes/no, and the transmission conductance UA. The charts show hours above 1000 ppm per room and mean temperature against window share. Few rooms have enough data for a real trend, so treat the scatter as a first look.

### 5. How the envelope is read from the IFC

Done once per building by `scripts/extract_room_envelope.py`:

1. For each storey, the outline (footprint) of all rooms is traced. Gaps up to about 8 m between rooms (corridors, stairs that are not modelled as rooms) are filled, so they do not count as "outside".
2. A room edge is **external** if it faces outside that footprint, or if a window sits on it.
3. Edges facing an enclosed courtyard or atrium are kept separately as **atrium side**.
4. Windows and doors are taken from the IFC geometry and matched to the nearest room edge within 0.8 m.
5. Each external edge gets a compass direction from its orientation. The model's +Y axis is assumed to be north unless you pass `--north-offset`.
6. Roof area is assigned to rooms on the top storey, and ground area to rooms on the ground storey.

### 6. Tab "Ventilation vs heat"

Compares air-change rates (now, 0.5, 1, 2, 4 per hour, and the rate needed to stay under the CO₂ limit):

$$C_{ss}=C_{out}+\frac{10^6\cdot P\cdot g}{n\,V} \qquad n_{req}=\frac{10^6\cdot P\cdot g}{(C_{limit}-C_{out})\,V}$$

with *P* people (you set the number) and *g* = 0.018 m³/h CO₂ per person (seated, light activity). The extra ventilation heat is $H_v\cdot\Delta T\cdot hours/1000$ per day, scaled to the heating season (your days per year, default 200). A **heat-recovery** slider reduces ventilation heat by that fraction. Ventilated hours per day default to the measured occupied hours.

### 7. Limits to keep in mind

- U-values are assumptions. Results scale directly with them, so compare rooms with the same settings rather than reading absolute kWh.
- The air-change rate needs clean CO₂ decays. Rooms with no clear decay fall back to the manual value (or you can borrow another room's rate).
- Steady state: no heat storage in the building mass, no solar gains, no internal gains from people or equipment.
- Open windows, doors and neighbouring rooms are not modelled separately; they appear only through the measured air-change rate.
- Sensor quality matters: drifting CO₂ sensors are flagged, but a temperature sensor next to a heater or window will bias the mean temperature.
- With long windows the data may be hourly means instead of raw readings when the fast copy of the database is in use (see the README).
