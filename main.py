import os
import asyncio
from datetime import datetime, timedelta
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Commute Decision Agent")

# 1. Geolocation Constants
ORIGIN_ADDRESS = "Im Rührets 4, 8803 Rüschlikon, Switzerland"
WAYPOINT_ADDRESS = "Kinderkrippe Nidelbad, Moosgartenweg 2, 8803 Rüschlikon"
DESTINATION_ADDRESS = "Sika, Tüffenwies 16-22, 8048 Zürich, Switzerland"

TRANSIT_DEPARTURE = "Thalwil (Bahnhof)"
TRANSIT_DESTINATION = "Zürich, Tüffenwies"

# Coordinates for APIs that require them (TomTom, Open-Meteo)
ORIGIN_COORDS = {"lat": 47.3090, "lon": 8.5562}
WAYPOINT_COORDS = {"lat": 47.3068, "lon": 8.5529}
DEST_COORDS = {"lat": 47.3934, "lon": 8.4907}

# Environment variables for API keys
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
TOMTOM_API_KEY = os.getenv("TOMTOM_API_KEY")


class CommuteDecision(BaseModel):
    verdict: str
    reason: str
    car_eta_mins: int
    train_eta_mins: int


async def get_google_drive_time(client: httpx.AsyncClient, with_kita: bool) -> int:
    """Returns drive time in minutes from Google Routes API."""
    if not GOOGLE_API_KEY:
        raise ValueError("GOOGLE_API_KEY is missing")

    url = "https://routes.googleapis.com/directions/v2:computeRoutes"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_API_KEY,
        "X-Goog-FieldMask": "routes.duration"
    }
    payload = {
        "origin": {"address": ORIGIN_ADDRESS},
        "destination": {"address": DESTINATION_ADDRESS},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE_OPTIMAL"
    }
    if with_kita:
        payload["intermediates"] = [{"address": WAYPOINT_ADDRESS}]
    
    response = await client.post(url, json=payload, headers=headers, timeout=10.0)
    response.raise_for_status()
    data = response.json()
    
    if not data.get("routes"):
        raise ValueError("No routes found by Google API")
        
    duration_str = data["routes"][0]["duration"]  # Format: "1800s"
    duration_sec = int(duration_str.rstrip("s"))
    return duration_sec // 60


async def get_tomtom_drive_time(client: httpx.AsyncClient, with_kita: bool) -> int:
    """Returns drive time in minutes from TomTom Routing API."""
    if not TOMTOM_API_KEY:
        raise ValueError("TOMTOM_API_KEY is missing")
        
    if with_kita:
        locations = f"{ORIGIN_COORDS['lat']},{ORIGIN_COORDS['lon']}:{WAYPOINT_COORDS['lat']},{WAYPOINT_COORDS['lon']}:{DEST_COORDS['lat']},{DEST_COORDS['lon']}"
    else:
        locations = f"{ORIGIN_COORDS['lat']},{ORIGIN_COORDS['lon']}:{DEST_COORDS['lat']},{DEST_COORDS['lon']}"
    url = f"https://api.tomtom.com/routing/1/calculateRoute/{locations}/json"
    params = {
        "key": TOMTOM_API_KEY,
        "computeTravelTimeFor": "all"
    }
    
    response = await client.get(url, params=params, timeout=10.0)
    response.raise_for_status()
    data = response.json()
    
    if not data.get("routes"):
        raise ValueError("No routes found by TomTom API")
        
    summary = data["routes"][0]["summary"]
    travel_time_sec = summary["travelTimeInSeconds"]
    return travel_time_sec // 60


async def get_transit_time(client: httpx.AsyncClient, with_kita: bool) -> int:
    """Returns transit time in minutes from Opendata.ch Transport API."""
    walking_overhead = 24 if with_kita else 15
    # Query time: now() + overhead
    departure_time = datetime.now() + timedelta(minutes=walking_overhead)
    
    url = "http://transport.opendata.ch/v1/connections"
    params = {
        "from": TRANSIT_DEPARTURE,
        "to": TRANSIT_DESTINATION,
        "date": departure_time.strftime("%Y-%m-%d"),
        "time": departure_time.strftime("%H:%M")
    }
    
    response = await client.get(url, params=params, timeout=10.0)
    response.raise_for_status()
    data = response.json()
    
    if not data.get("connections"):
        raise ValueError("No transit connections found")
        
    connection = data["connections"][0]
    
    # Parse duration format (e.g. "00d00:45:00")
    duration_str = connection.get("duration", "00d00:00:00")
    parts = duration_str.split("d")
    time_part = parts[1] if len(parts) > 1 else parts[0]
    h, m, s = map(int, time_part.split(":"))
    transit_duration_mins = h * 60 + m
    
    # Extract departure time
    departure_str = connection.get("from", {}).get("departure")
    train_departure_time = ""
    mins_until_departure = 0
    if departure_str:
        try:
            # Format usually looks like: "2026-10-04T08:34:00+0200"
            parsed_time = datetime.strptime(departure_str[:16], "%Y-%m-%dT%H:%M")
            train_departure_time = parsed_time.strftime("%I:%M %p")
            
            dep_ts = connection.get("from", {}).get("departureTimestamp")
            if dep_ts:
                now_ts = datetime.now().timestamp()
                mins_until_departure = int((dep_ts - now_ts) / 60)
        except Exception:
            train_departure_time = departure_str[11:16] # fallback to raw HH:MM substring
            
    # Extract arrival time
    arrival_str = connection.get("to", {}).get("arrival")
    train_arrival_time = ""
    if arrival_str:
        try:
            parsed_arr_time = datetime.strptime(arrival_str[:16], "%Y-%m-%dT%H:%M")
            train_arrival_time = parsed_arr_time.strftime("%I:%M %p")
        except Exception:
            train_arrival_time = arrival_str[11:16]
            
    # Calculate delays if any
    delay = 0
    if connection.get("from") and connection["from"].get("delay"):
        delay_val = connection["from"]["delay"]
        if isinstance(delay_val, str) and delay_val.isdigit():
            delay = int(delay_val)
        elif isinstance(delay_val, int):
            delay = delay_val
            
    # Extract route combinations
    legs = []
    for sec in connection.get("sections", []):
        journey = sec.get("journey")
        if journey:
            cat = journey.get("category", "")
            num = journey.get("number", "")
            legs.append(f"{cat}{num}".strip())
        elif sec.get("walk"):
            if not legs or legs[-1] != "Walk":
                legs.append("Walk")
    combo_str = " -> ".join(legs) if legs else "Direct"
            
    return (transit_duration_mins + delay, train_departure_time, combo_str, mins_until_departure, train_arrival_time)


async def get_weather(client: httpx.AsyncClient) -> tuple[float, float]:
    """Returns (precipitation_mm, temperature_c) from Open-Meteo for the current hour."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": ORIGIN_COORDS["lat"],
        "longitude": ORIGIN_COORDS["lon"],
        "current": "temperature_2m,precipitation"
    }
    
    response = await client.get(url, params=params, timeout=10.0)
    response.raise_for_status()
    data = response.json()
    
    current = data.get("current", {})
    temp = current.get("temperature_2m", 0.0)
    precip = current.get("precipitation", 0.0)
    
    return precip, temp


@app.get("/decide", response_model=CommuteDecision)
async def evaluate_commute(with_kita: bool = False):
    async with httpx.AsyncClient() as client:
        try:
            # Execute API calls concurrently
            results = await asyncio.gather(
                get_google_drive_time(client, with_kita),
                get_tomtom_drive_time(client, with_kita),
                get_transit_time(client, with_kita),
                get_weather(client),
                return_exceptions=True
            )
            
            google_duration = results[0]
            tomtom_duration = results[1]
            transit_duration = results[2]
            weather_result = results[3]
            
            # 1. Car Time Evaluation
            car_time = None
            if isinstance(google_duration, Exception):
                print(f"Google API Failed: {google_duration}")
                # Fallback to TomTom if Google fails
                if isinstance(tomtom_duration, Exception):
                    print(f"TomTom API Failed: {tomtom_duration}")
                    raise HTTPException(status_code=500, detail="Both routing APIs failed")
                car_time = tomtom_duration
            else:
                car_time = google_duration
                # Cross-reference with TomTom ETA
                if not isinstance(tomtom_duration, Exception):
                    if tomtom_duration > (google_duration * 1.10):
                        car_time = tomtom_duration  # Use pessimistic ETA if >10% divergence
            
            car_dropoff_overhead = 5 if with_kita else 0
            total_car_time = car_time + car_dropoff_overhead
            
            # 2. Transit Time Evaluation
            if isinstance(transit_duration, Exception):
                raise HTTPException(status_code=500, detail=f"Transit API failed: {str(transit_duration)}")
                
            raw_transit_duration, train_departure_time, combo_str, mins_until_departure, train_arrival_time = transit_duration
            
            # Total time from NOW until arrival at destination via train
            train_total_time_from_now = mins_until_departure + raw_transit_duration
            
            # 3. Weather Evaluation & Penalty
            precip, temp = (0.0, 10.0)
            if not isinstance(weather_result, Exception):
                precip, temp = weather_result
            else:
                print(f"Warning: Weather API failed: {weather_result}")
            
            weather_penalty = 0
            if precip > 0 or temp < 1.0:
                weather_penalty = 10
                
            penalized_transit_time = train_total_time_from_now + weather_penalty
            
            # 4. Decision Logic & Thresholds
            transit_threshold = 10  # Transit must be at least 10 minutes faster to win
            
            # Traffic Anomaly Logic: If car ETA exceeds 35 mins by >15 mins (i.e. > 50 mins)
            if total_car_time > 50:
                transit_threshold = 0
                
            # Calculations for text output
            car_arrival_time = (datetime.now() + timedelta(minutes=total_car_time)).strftime("%I:%M %p")
            time_saved = abs(train_total_time_from_now - total_car_time)
            
            reasoning = []
            if weather_penalty > 0:
                reasoning.append(f"🌧️ Weather Warning: Rain/Freeze detected (+10m transit penalty)\n")
            if transit_threshold == 0:
                reasoning.append(f"⚠️ Traffic Anomaly: Car time > 50m! Threshold reduced to 0m.\n")
                
            if penalized_transit_time <= total_car_time - transit_threshold:
                verdict = "TRAIN"
                reason_str = (
                    f"🚆 Train ETA: {train_total_time_from_now}m (Saves {time_saved}m!)\n"
                    f"⏰ Leaves in: {mins_until_departure}m (at {train_departure_time})\n"
                    f"🗺️ Route: {combo_str} ({raw_transit_duration}m ride)\n"
                    f"🏁 Arrive at Sika: {train_arrival_time}\n"
                    f"\n"
                    f"🚙 Car ETA: {total_car_time}m (Arrive {car_arrival_time})"
                )
                reasoning.append(reason_str)
            else:
                verdict = "CAR"
                reason_str = (
                    f"🚙 Car ETA: {total_car_time}m (Arrive {car_arrival_time})\n"
                    f"⏱️ Saves: {time_saved}m over transit!\n"
                    f"\n"
                    f"🚆 Next Train: {train_departure_time} (in {mins_until_departure}m)\n"
                    f"🗺️ Route: {combo_str} ({raw_transit_duration}m ride)\n"
                    f"🏁 Train Arrival: {train_arrival_time}"
                )
                reasoning.append(reason_str)
                
            return CommuteDecision(
                verdict=verdict,
                reason="".join(reasoning),
                car_eta_mins=total_car_time,
                train_eta_mins=train_total_time_from_now
            )
            
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
