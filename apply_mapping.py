#!/usr/bin/env python
"""Apply CO2 sensor mapping to the database - optimized version."""
import pandas as pd
from temperature_dashboard.db import connect, normalize_mapping

def main():
    # Load mapping
    m = pd.read_csv('data/co2_location_mapping.csv', dtype=str)
    mn = normalize_mapping(m)
    
    # Connect to CO2 database
    con = connect('CO2')
    
    # Create a temporary mapping table for efficient JOIN
    con.execute("DROP TABLE IF EXISTS temp_mapping")
    con.execute("CREATE TEMP TABLE temp_mapping (sensor TEXT PRIMARY KEY, building TEXT, room TEXT)")
    
    # Insert mapping data
    params = [(r.sensor, r.building, r.room) for r in mn.itertuples()]
    con.executemany("INSERT INTO temp_mapping (sensor, building, room) VALUES (?, ?, ?)", params)
    print(f'Inserted {len(params)} mapping entries')
    
    # Update using correlated subquery (SQLite compatible)
    cur = con.execute("""
        UPDATE readings 
        SET building = (SELECT building FROM temp_mapping tm WHERE tm.sensor = readings.sensor),
            room = (SELECT room FROM temp_mapping tm WHERE tm.sensor = readings.sensor)
        WHERE sensor IN (SELECT sensor FROM temp_mapping)
        AND (building IS NULL OR building = 'Unassigned' OR building = '')
    """)
    con.commit()
    print(f'Updated {con.total_changes} rows')
    
    # Verify
    mapped_count = con.execute("SELECT COUNT(*) FROM readings WHERE building != 'Unassigned'").fetchone()[0]
    print(f'Mapped readings: {mapped_count}')
    
    distinct_mapped = con.execute("SELECT COUNT(DISTINCT sensor) FROM readings WHERE building != 'Unassigned'").fetchone()[0]
    print(f'Mapped sensors: {distinct_mapped}')

if __name__ == '__main__':
    main()