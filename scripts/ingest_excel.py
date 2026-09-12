import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from temperature_dashboard.ingest import ingest_temperature_excel
import sys
p=Path(sys.argv[1])
with p.open('rb') as f:
    print(ingest_temperature_excel(f,p.name))
