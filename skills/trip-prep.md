---
description: Pull together calendar, weather, and packing notes for an upcoming trip.
---

When Craig asks you to help prep for a trip on given dates:

1. Call `get_events_by_date` for the trip's date range to see what's already
   scheduled (flights, meetings, reservations).
2. Call `fetch_weather` for the destination, passing enough `days` to cover the
   trip if it's within the 5-day forecast window; otherwise say the forecast
   doesn't reach that far yet.
3. Summarize in this order: the itinerary from the calendar, then the weather
   outlook, then a short packing suggestion driven by the weather (e.g. rain →
   pack a shell). Keep it tight — Craig wants the shape of the trip, not a wall
   of detail.
