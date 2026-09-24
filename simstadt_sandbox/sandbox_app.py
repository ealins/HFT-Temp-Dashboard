import streamlit as st
import pandas as pd
import numpy as np
import subprocess
import os
import glob
import time
import sys

# Ensure the root directory is in the Python path so we can import temperature_dashboard
try:
    import temperature_dashboard
except ImportError:
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from temperature_dashboard.db import query, exists, hierarchy

# 1. Sensor Data Pre-processing Logic
@st.cache_data(show_spinner=False)
def model_occupancy(co2_df, threshold_ppm: float = 800.0):
    # Model: If CO2 > threshold_ppm, room is occupied (1), else unoccupied (0)
    co2_df = co2_df.copy()
    co2_df['occupancy'] = (co2_df['value'] > threshold_ppm).astype(int)
    return co2_df

@st.cache_data(show_spinner=False)
def model_ach(co2_df):
    # Model: ACH based on CO2 decay rate (simple approximation)
    co2_df = co2_df.copy()
    # Calculate CO2 change rate
    co2_df['co2_diff'] = co2_df['value'].diff()
    # Vectorized calculation for performance instead of .apply()
    co2_df['ach'] = np.where(co2_df['co2_diff'] < 0, co2_df['co2_diff'].abs() / 100.0, 0.1)
    return co2_df

@st.cache_data(show_spinner=False)
def model_thermostat(temp_df):
    # Model: Average temperature as the dynamic thermostat setpoint
    return temp_df['value'].mean()

@st.cache_data(show_spinner="Extracting Boundary Conditions...")
def get_simstadt_boundary_conditions(building, temp_room, co2_room, start, end, co2_threshold: float = 800.0):
    co2_df = query("CO₂", building, co2_room, start=start, end=end)
    temp_df = query("Temperature", building, temp_room, start=start, end=end)
    
    if co2_df.empty or temp_df.empty:
        return pd.DataFrame(), pd.DataFrame(), None
        
    co2_df = model_occupancy(co2_df, threshold_ppm=co2_threshold)
    co2_df = model_ach(co2_df)
    thermostat_setpoint = model_thermostat(temp_df)
    
    return co2_df, temp_df, thermostat_setpoint

# 2. Async Simulation Engine Execution
def run_simstadt():
    # Placeholder for the actual SimStadt CLI command
    # e.g. cmd = ["java", "-jar", "SimStadt.jar", "-i", "input", "-o", "outputs"]
    # subprocess.run(cmd, capture_output=True, text=True, check=True)
    
    try:
        # Simulating the time it takes to run a simulation headless
        time.sleep(1.5)
        
        # Simulate output file generation
        outputs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")
        os.makedirs(outputs_dir, exist_ok=True)
        with open(os.path.join(outputs_dir, "results.csv"), "w") as f:
            f.write("time,heat_demand\n1,100\n2,150\n3,120")
        return True, "Simulation Successful"
    except Exception as e:
        return False, str(e)

def get_available_rooms(kind: str, building: str, floor: str):
    if not exists(kind):
        return []
    h = hierarchy(kind)
    mask = h["building"].astype(str) == str(building)
    if floor is not None:
        mask &= h["floor"].astype(str) == str(floor)
    return sorted(h.loc[mask, "room"].astype(str).unique().tolist())

# 3. Streamlit UI
def render_simstadt_sandbox(building: str, floor: str, start: pd.Timestamp = None, end: pd.Timestamp = None):
    st.header(f"SimStadt Sandbox: Sensor-Driven Simulation for Building {building} (Floor {floor})")

    st.markdown(r"""
    ### 🔬 What modeling can be done with your sensor data?
    By utilizing the historical **Temperature** and **CO₂** data already ingested in the database, we can calibrate SimStadt's physical assumptions:
    - **Occupancy & Internal Heat Gains ($Q_I$)**: Use CO₂ peaks as a proxy for human occupancy schedules instead of standard SIA/DIN assumptions.
    - **Ventilation Losses ($Q_V$)**: Infer true Air Changes per Hour (ACH) from CO₂ decay rates.
    - **Transmission Losses ($Q_T$)**: Use actual room temperatures for the internal thermostat setpoint to calculate accurate $\Delta T$ against outside weather.
    """)

    if 'simulation_running' not in st.session_state:
        st.session_state.simulation_running = False

    st.sidebar.header("1. Sensor Data Selection")
    
    # Use existing data from DB
    temp_rooms = get_available_rooms("Temperature", building, floor)
    co2_rooms = get_available_rooms("CO₂", building, floor)
    
    selected_temp_room = st.sidebar.selectbox("Select Temperature Room", temp_rooms) if temp_rooms else None
    selected_co2_room = st.sidebar.selectbox("Select CO₂ Room", co2_rooms) if co2_rooms else None

    co2_threshold = st.sidebar.slider(
        "CO₂ Occupancy Threshold (ppm)",
        min_value=450,
        max_value=1500,
        value=800,
        step=25,
        help="CO₂ concentration above this value indicates human occupancy."
    )

    if not temp_rooms and not co2_rooms:
        st.sidebar.warning("No sensor data available for this building/floor. Please upload data via Admin panel.")

    # --- Render the Modeling Results ---
    st.markdown("---")
    st.subheader("📊 Derived SimStadt Boundary Conditions")
    
    if selected_co2_room and selected_temp_room:
        co2_df, temp_df, avg_temp = get_simstadt_boundary_conditions(
            building, selected_temp_room, selected_co2_room, start, end, co2_threshold=float(co2_threshold)
        )
        
        if not co2_df.empty and not temp_df.empty:
            co2_min, co2_max = co2_df['value'].min(), co2_df['value'].max()
            st.caption(f"Observed room CO₂ range: **{co2_min:.0f} ppm – {co2_max:.0f} ppm** (Threshold: **{co2_threshold} ppm**)")
            
            col1, col2, col3 = st.columns(3)
            col1.metric("Dynamic Thermostat Setpoint", f"{avg_temp:.1f} °C")
            
            # Calculate occupied hours
            time_diffs = co2_df['timestamp'].diff().dt.total_seconds().mean() / 3600.0
            occupied_hours = co2_df['occupancy'].sum() * (time_diffs if pd.notnull(time_diffs) else 0)
            col2.metric("Inferred Occupied Hours", f"{occupied_hours:.0f} h")
            
            avg_ach = co2_df['ach'].mean()
            col3.metric("Average Air Changes (ACH)", f"{avg_ach:.2f} h⁻¹")

            # Resample for plotting to avoid freezing the browser with millions of points
            st.markdown("#### Occupancy Profile (Internal Heat Gains)")
            
            # Select only relevant numeric columns to avoid aggregation errors
            plot_data = co2_df.set_index('timestamp')[['occupancy', 'ach']]
            
            if len(plot_data) > 5000:
                resampled_data = plot_data.resample('1h').mean()
            else:
                resampled_data = plot_data
                
            st.line_chart(resampled_data['occupancy'])

            st.markdown("#### Ventilation Profile (ACH from CO₂ decay)")
            st.line_chart(resampled_data['ach'])
        else:
            st.info("Insufficient data to render models for the selected rooms.")

    st.sidebar.header("2. Calibration & Retrofit Scenarios")
    window_upgrade = st.sidebar.selectbox("Window Glazing Upgrade", ["None", "Double Glazing (U=1.2)", "Triple Glazing (U=0.8)"])
    wall_insulation = st.sidebar.slider("Add Exterior Insulation Thickness (cm)", 0, 20, 0)

    st.sidebar.header("3. Validation Data")
    actual_energy_use = st.sidebar.number_input("Actual Metered Heat Use (kWh/year)", value=15000)

    st.sidebar.header("4. Simulation Control")
    if st.sidebar.button("▶ Run Local SimStadt Simulation"):
        st.session_state.simulation_running = True
        with st.spinner("Simulating..."):
            success, message = run_simstadt()
            st.session_state.simulation_running = False
            if success:
                st.sidebar.success(message)
            else:
                st.sidebar.error(f"Error: {message}")

    # 5. Result Rendering & Validation
    outputs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")
    output_files = glob.glob(os.path.join(outputs_dir, "*.csv"))

    if output_files:
        st.header("Calibration & Validation Dashboard")
        
        # Calculate Discrepancy based on UI input
        simulated_heat_demand = 12000 # Mock value from output
        discrepancy = ((simulated_heat_demand - actual_energy_use) / actual_energy_use) * 100
        
        # Display Tiles
        col1, col2, col3 = st.columns(3)
        col1.metric("Simulated Heat Demand", f"{simulated_heat_demand} kWh", delta=f"{simulated_heat_demand - actual_energy_use} kWh vs Actual", delta_color="inverse")
        col2.metric("Actual Metered Use", f"{actual_energy_use} kWh")
        col3.metric("Calibration Discrepancy", f"{discrepancy:.1f}%")
        
        st.divider()
        
        st.subheader("Simulated Heat Demand vs Real-world Telemetry")
        # Line Chart
        df = pd.read_csv(output_files[0])
        st.line_chart(df.set_index('time'))
    else:
        st.info("No simulation results found. Please configure scenarios and run the simulation.")

if __name__ == "__main__":
    st.set_page_config(layout="wide")
    # Provide default building and floor for standalone testing
    render_simstadt_sandbox("1", "EG")
