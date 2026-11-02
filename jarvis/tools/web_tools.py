import requests
from google.genai import types

from .base import ctx, tool

WMO = {0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain",
       65: "heavy rain", 71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
       81: "rain showers", 82: "violent rain showers", 95: "a thunderstorm", 96: "a thunderstorm with hail",
       99: "a thunderstorm with hail"}


@tool
def get_weather(city: str = "") -> str:
    """Current weather and today's forecast for a city. Leave city blank for the user's home city."""
    city = city or ctx.cfg.assistant.home_city
    geo = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                       params={"name": city, "count": 1}, timeout=10).json()
    if not geo.get("results"):
        return f"Error: could not find a place called {city}."
    place = geo["results"][0]
    w = requests.get("https://api.open-meteo.com/v1/forecast", timeout=10, params={
        "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto", "forecast_days": 1,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
    }).json()
    cur, day = w["current"], w["daily"]
    return (f"{place['name']}: {cur['temperature_2m']:.0f} C, feels like {cur['apparent_temperature']:.0f}, "
            f"{WMO.get(cur['weather_code'], 'unsettled')}, wind {cur['wind_speed_10m']:.0f} km/h. "
            f"Today {day['temperature_2m_min'][0]:.0f} to {day['temperature_2m_max'][0]:.0f} C, "
            f"{day['precipitation_probability_max'][0]}% chance of rain.")


@tool
def web_search(query: str) -> str:
    """Search the web with Google for current information, news, facts, scores or prices, and return a summary."""
    resp = ctx.client.models.generate_content(
        model=ctx.cfg.gemini.model,
        contents=query,
        config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]),
    )
    return resp.text or "No results."
