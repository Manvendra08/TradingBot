import requests
import csv
import io

MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

def main():
    print("Downloading master list...")
    r = requests.get(MASTER_URL, timeout=30)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    
    naturalgas_rows = []
    crudeoil_rows = []
    
    for row in reader:
        symbol = str(row.get("SEM_TRADING_SYMBOL") or "").upper()
        seg = str(row.get("SEM_EXM_EXCH_ID") or "").upper()
        instru = str(row.get("SEM_INSTRUMENT_NAME") or "").upper()
        
        if "MCX" in seg:
            if "NATURALGAS" in symbol:
                naturalgas_rows.append(row)
            elif "CRUDEOIL" in symbol:
                crudeoil_rows.append(row)
                
    print("\n--- NATURALGAS MCX Rows ---")
    for r in naturalgas_rows:
        print(f"Symbol: {r.get('SEM_TRADING_SYMBOL')}, ID: {r.get('SEM_SMST_SECURITY_ID')}, Exch: {r.get('SEM_EXM_EXCH_ID')}, Instrument: {r.get('SEM_INSTRUMENT_NAME')}, Expiry: {r.get('SEM_EXPIRY_DATE')}")
        
    print("\n--- CRUDEOIL MCX Rows ---")
    for r in crudeoil_rows:
        print(f"Symbol: {r.get('SEM_TRADING_SYMBOL')}, ID: {r.get('SEM_SMST_SECURITY_ID')}, Exch: {r.get('SEM_EXM_EXCH_ID')}, Instrument: {r.get('SEM_INSTRUMENT_NAME')}, Expiry: {r.get('SEM_EXPIRY_DATE')}")

if __name__ == "__main__":
    main()
