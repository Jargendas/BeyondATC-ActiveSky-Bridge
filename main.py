import asyncio
import gzip
import warnings
import subprocess
import requests
import io
from requests_toolbelt.adapters import host_header_ssl
from uvicorn import Config, Server
from pathlib import Path
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from watchfiles import awatch
from metar import Metar
from datetime import datetime
import xml.etree.ElementTree as ET

# Constants
METAR_FILE = Path(Path.home() / "AppData/Roaming/HiFi/AS_FS/Weather/current_wx_snapshot.txt") # Path to your ActiveSky weather snapshot file
CERT_FILE = Path("cert.pem") # Cert file for https certificate
KEY_FILE = Path("key.pem") # Key file for https certificate
DNS_IP = "1.1.1.1" # Path to a DNS server of your choice to look up the actual IP of aviationweather.gov

CACHE_FILE = Path("metars.cache.xml.gz") # Name of the cache file to be provided to BeyondATC - should not be changed

# Actual aviationweather.gov IP - will be requested automatically
aviationweather_IP = ""

# METAR data from ActiveSky
metar_data = {}
taf_data = {}
wind_data = {}

# FastAPI application
app = FastAPI()

def generate_xml(metars, datasource="metars"):
    try:
        # Root of the XML document
        root = ET.Element("response", attrib={
            "xmlns:xsd": "http://www.w3.org/2001/XMLSchema",
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "version": "1.3",
            "xsi:noNamespaceSchemaLocation": f"https://aviationweather.gov/data/schema/{datasource[:-1]}1_3.xsd",
        })
        ET.SubElement(root, "request_index").text = str(int(datetime.now().timestamp()))
        ET.SubElement(root, "data_source", attrib={"name": datasource})
        ET.SubElement(root, "request", attrib={"type": "retrieve"})
        ET.SubElement(root, "errors")
        ET.SubElement(root, "warnings")
        ET.SubElement(root, "time_taken_ms").text = "5"
        data = ET.SubElement(root, "data", attrib={"num_results": str(len(metars))})

        for metar_text in metars.values():
            try:
                # Parse data
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    metar = Metar.Metar(metar_text, strict=False)
                metar_element = ET.SubElement(data, datasource[:-1].upper())
                if (datasource == "tafs"):
                    metar_text = "TAF " + metar_text
                ET.SubElement(metar_element, "raw_text").text = metar_text
                ET.SubElement(metar_element, "station_id").text = metar.station_id
                if (datasource == "tafs"):
                    ET.SubElement(metar_element, "issue_time").text = metar.time.isoformat() + "Z"
                else:
                    ET.SubElement(metar_element, "observation_time").text = metar.time.isoformat() + "Z"
                ET.SubElement(metar_element, "latitude").text = ""
                ET.SubElement(metar_element, "longitude").text = ""
                if (metar.temp):
                    ET.SubElement(metar_element, "temp_c").text = str(int(round(metar.temp.value("C"))))
                if (metar.dewpt):
                    ET.SubElement(metar_element, "dewpoint_c").text = str(int(round(metar.dewpt.value("C"))))
                if (metar.wind_dir):
                    ET.SubElement(metar_element, "wind_dir_degrees").text = str(int(round(metar.wind_dir.value())))
                if (metar.wind_speed):
                    ET.SubElement(metar_element, "wind_speed_kt").text = str(int(round(metar.wind_speed.value("KT"))))
                if (metar.wind_gust):
                    ET.SubElement(metar_element, "wind_gust_kt").text = str(int(round(metar.wind_gust.value("KT"))))
                visibility = 9999
                if (metar.vis):
                    visibility = int(round(metar.vis.value("MI")))
                    ET.SubElement(metar_element, "visibility_statute_mi").text = str(visibility)
                if (metar.press):
                    ET.SubElement(metar_element, "altim_in_hg").text = str(round(metar.press.value("IN"), 2))
                if (len(metar.weather) > 0):
                    ET.SubElement(metar_element, "wx_string").text = str((metar.weather[0][0] or "") + (metar.weather[0][2] or "") + (metar.weather[0][3] or ""))
                ceiling = 40000
                for cond in metar.sky:
                    if cond[1]:
                        height = int(round(cond[1].value("FT")))
                        ET.SubElement(metar_element, "sky_condition", {"sky_cover": str(cond[0]), "cloud_base_ft_agl": str(height)})
                        if ((cond[0] == "BKN") or (cond[0] == "OVC")) and (height < ceiling):
                            ceiling = height
                    else:
                        ET.SubElement(metar_element, "sky_condition", {"sky_cover": str(cond[0])})

                fc_element = ET.SubElement(metar_element, "flight_category")
                if (visibility > 5) and (ceiling > 3000):
                    fc_element.text = "VFR"
                elif ((visibility >= 3) and (visibility <= 5)) or ((ceiling >= 1000) and (ceiling <= 3000)):
                    fc_element.text = "MVFR"
                elif ((visibility >= 1) and (visibility <= 3)) or ((ceiling >= 500) and (ceiling <= 1000)):
                    fc_element.text = "IFR"
                else:
                    fc_element.text = "LIFR"

                if (metar.precip_1hr):
                    ET.SubElement(metar_element, "precip_in").text = str(round(metar.precip_1hr.value("IN"), 3))
                if (metar.precip_3hr):
                    ET.SubElement(metar_element, "pcp3hr_in").text = str(round(metar.precip_3hr.value("IN"), 3))
                if (metar.precip_6hr):
                    ET.SubElement(metar_element, "pcp6hr_in").text = str(round(metar.precip_6hr.value("IN"), 3))
                if (metar.precip_24hr):
                    ET.SubElement(metar_element, "pcp24hr_in").text = str(round(metar.precip_24hr.value("IN"), 3))
                ET.SubElement(metar_element, "metar_type").text = "METAR"
                #ET.SubElement(metar_element, "elevation_m").text = ""
            except Exception as e:
                print(f"Error parsing METAR/TAF: {metar_text.strip()} - {e}")

        # Return XML data
        tree = ET.ElementTree(root)
        ET.indent(tree)

        return tree

    except Exception as e:
        print(f"Error generating the XML file: {e}")

    return None

def parse_current_wx_file(filepath: Path) -> None:
    """Parses the current_wx file from ActiveSky and generates a gzipped XML file."""
    
    global metar_data, taf_data, wind_data

    # Parse lines
    if filepath.exists():
        with filepath.open("r") as f:
            lines = f.readlines()
        
        for line in lines:
            parts = line.split("::")
            station_id = parts[0].strip().lower()
            metar_text = parts[1].strip()
            metar_data[station_id] = metar_text
            if (len(parts) > 2):
                taf_text = parts[2].strip()
                taf_data[station_id] = taf_text
            if (len(parts) > 3):
                wind_text = parts[3].strip()
                wind_data[station_id] = wind_text

# Requests from BeyondATC/Fenix
@app.get("/data/cache/metars.cache.xml.gz")
async def get_metar_cache():
    """Serves the gzipped METAR XML file."""
    if CACHE_FILE.exists():
        print("Serving weather data to BeyondATC...")
        return FileResponse(CACHE_FILE, media_type="application/gzip")
    return {"error": "Cache file not found."}

@app.get("/cgi-bin/data/dataserver.php")
@app.get("/cgi-bin/data/dataserver")
async def request_data(request: Request):
    """Serves specific METARs"""
    if (request.query_params["format"].lower() == "xml"):
        stationString = request.query_params["stationString"].lower()
        if (request.query_params["dataSource"].lower() == "metars"):
            if stationString in metar_data:
                print(f"Serving METAR for {stationString}...")
                tree = generate_xml({stationString: metar_data[stationString]})
                return Response(content=ET.tostring(tree.getroot(), encoding="utf-8"), status_code=200, media_type="application/xml")
            
        if (request.query_params["dataSource"] == "tafs"):
            if stationString in taf_data:
                print(f"Serving TAF for {stationString}...")
                tree = generate_xml({stationString: taf_data[stationString]}, "tafs")
                return Response(content=ET.tostring(tree.getroot(), encoding="utf-8"), status_code=200, media_type="application/xml")
            
    # Fallback
    return await aviationweather_proxy(request, "cgi-bin/data/dataserver.php")

# Everything else is forwarded to aviationweather.gov
@app.api_route("/{path_name:path}", methods=["GET"])
async def aviationweather_proxy(request: Request, path_name: str):
    session = requests.Session()
    session.mount('https://', host_header_ssl.HostHeaderSSLAdapter())
    print(f"Getting data from https://{aviationweather_IP}/{path_name}...")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        resp = session.get(f"https://{aviationweather_IP}/{path_name}", headers={"Host": "aviationweather.gov"}, params=request.query_params, verify=False)

    return Response(content=resp.content, status_code=resp.status_code)

def generate_metar_cache():
    parse_current_wx_file(METAR_FILE)
    tree = generate_xml(metar_data)
    with gzip.open(CACHE_FILE, "wb") as f:
        tree.write(f, encoding="utf-8", xml_declaration=True)

async def watch_metar_file():
    """Watches the METAR file and updates the cache on changes."""
    print("Started watching the METAR file...")
    async for _ in awatch(METAR_FILE):
        print("ActiveSky weather change detected, updating cache...")
        generate_metar_cache()

def find_aviationweather_IP():
    print("Looking up actual aviationweather.gov IP...")
    
    # Use Google's DNS server directly to bypass local DNS
    nslookup_data = subprocess.check_output(["nslookup", "aviationweather.gov", DNS_IP]).decode().split("\n")
    
    ip = ""
    for line in nslookup_data:
        if "Address" in line:
            if DNS_IP not in line and "." in line:  # Exclude DNS server address and IPv6
                ip = line.split(" ")[-1].strip()
                print(f"Found IP: {ip}")
                break

    if ip == "":
        print("No IP could be found!")
        
    return ip

async def main():
    """Main entry point for the application."""

    # Generate initial cache
    print("Initializing cache from ActiveSky data...")
    generate_metar_cache()

    # Find actual aviationweather.gov IP to supply to ActiveSky if needed
    global aviationweather_IP
    aviationweather_IP = find_aviationweather_IP()

    # Start file watcher in a background task
    asyncio.create_task(watch_metar_file())

    # Run the FastAPI server
    config = Config(
        app=app,
        host="127.0.0.1",
        port=443,
        ssl_certfile=str(CERT_FILE),
        ssl_keyfile=str(KEY_FILE),
        loop=asyncio.get_event_loop(),
    )
    server = Server(config)
    try:
        await server.serve()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    loop = asyncio.new_event_loop()
    loop.run_until_complete(main())