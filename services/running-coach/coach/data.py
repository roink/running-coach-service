"""Collection-scoped, bounded MongoDB reads; never accepts query operators."""

import datetime
import hmac
import json
import os
from pathlib import Path

from aiohttp import web
from pymongo import MongoClient

SUMMARY_FIELDS = {
    "_id": 0,
    "id": 1,
    "garmin_activity_id": 1,
    "source": 1,
    "type": 1,
    "sport_type": 1,
    "name": 1,
    "description": 1,
    "timezone": 1,
    "average_temp": 1,
    "perceived_exertion": 1,
    "workout_type": 1,
    "start_date": 1,
    "start_date_local": 1,
    "distance": 1,
    "moving_time": 1,
    "elapsed_time": 1,
    "total_elevation_gain": 1,
    "average_speed": 1,
    "max_speed": 1,
    "average_heartrate": 1,
    "max_heartrate": 1,
    "average_cadence": 1,
    "average_watts": 1,
    "synced_at": 1,
    "garmin_synced_at": 1,
}


def utc_date(text):
    value = datetime.date.fromisoformat(text)
    return value.isoformat() + "T00:00:00Z"


def bounded_limit(text, maximum=200):
    value = int(text)
    if not 1 <= value <= maximum:
        raise ValueError("Limit outside permitted range")
    return value


def response(data):
    encoded = json.dumps(data, default=str, allow_nan=False)
    if len(encoded.encode()) > 8 * 1024 * 1024:
        raise web.HTTPRequestEntityTooLarge(max_size=8 * 1024 * 1024, actual_size=len(encoded))
    return web.Response(text=encoded, content_type="application/json")


class DataService:
    def __init__(self, uri):
        self.mongo = MongoClient(
            uri,
            serverSelectionTimeoutMS=5000,
            socketTimeoutMS=15_000,
            maxPoolSize=3,
            appname="nas-running-coach-read-service",
        )
        self.activities = self.mongo["strava"]["activities"]

    async def inventory(self, request):
        if set(request.query) - {"after", "limit"}:
            raise web.HTTPBadRequest()
        after = int(request.query.get("after", "0"))
        limit = bounded_limit(request.query.get("limit", "500"), maximum=500)
        rows = list(
            self.activities.find(
                {"id": {"$gt": after}},
                {"_id": 0, "id": 1, "type": 1, "synced_at": 1, "garmin_synced_at": 1},
            )
            .sort("id", 1)
            .limit(limit)
            .max_time_ms(5000)
        )
        return response(
            {"activities": rows, "next": rows[-1]["id"] if len(rows) == limit else None}
        )

    async def history(self, request):
        if set(request.query) - {"start", "end", "before", "limit"}:
            raise web.HTTPBadRequest()
        start = utc_date(request.query["start"])
        end = utc_date(request.query["end"])
        if (
            start >= end
            or (
                datetime.date.fromisoformat(request.query["end"])
                - datetime.date.fromisoformat(request.query["start"])
            ).days
            > 366
        ):
            raise ValueError("Date range must contain 1–366 days; end is exclusive")
        limit = bounded_limit(request.query.get("limit", "100"))
        query = {"start_date": {"$gte": start, "$lt": end}}
        if request.query.get("before"):
            query["id"] = {"$lt": int(request.query["before"])}
        rows = list(
            self.activities.find(query, SUMMARY_FIELDS)
            .sort("id", -1)
            .limit(limit)
            .max_time_ms(5000)
        )
        return response(
            {"activities": rows, "next": rows[-1]["id"] if len(rows) == limit else None}
        )

    async def activity(self, request):
        if set(request.query) - {"streams"} or request.query.get("streams", "false") not in {
            "true",
            "false",
        }:
            raise web.HTTPBadRequest()
        activity_id = int(request.match_info["id"])
        fields = {**SUMMARY_FIELDS, "laps": 1, "splits_metric": 1, "splits_standard": 1}
        for metric in [
            "aerobicTrainingEffect",
            "anaerobicTrainingEffect",
            "trainingEffectLabel",
            "activityTrainingLoad",
            "avgStrideLength",
            "avgGroundContactTime",
            "avgVerticalOscillation",
            "avgVerticalRatio",
            "vO2MaxValue",
            "maxTemperature",
            "minTemperature",
            "avgTemperature",
            "averageRPE",
        ]:
            fields["source_data.garmin.summary." + metric] = 1
        if request.query.get("streams") == "true":
            for namespace in ["streams", "garmin_streams"]:
                for field in [
                    "time",
                    "heartrate",
                    "velocity_smooth",
                    "distance",
                    "altitude",
                    "cadence",
                    "watts",
                    "moving",
                ]:
                    fields[f"{namespace}.{field}.data"] = {"$slice": 100_000}
        document = self.activities.find_one({"id": activity_id}, fields, max_time_ms=5000)
        if document is None:
            raise web.HTTPNotFound()
        return response(document)


def main():
    config = json.loads(Path(os.environ["COACH_DATA_CONFIG"]).read_text())
    service = DataService(config["mongo_uri"])

    @web.middleware
    async def authorize(request, handler):
        if not hmac.compare_digest(
            request.headers.get("Authorization", ""), "Bearer " + config["token"]
        ):
            raise web.HTTPUnauthorized()
        try:
            return await handler(request)
        except (ValueError, KeyError):
            raise web.HTTPBadRequest(text="Invalid bounded read parameters") from None

    app = web.Application(middlewares=[authorize], client_max_size=1024)
    app.router.add_get("/inventory", service.inventory)
    app.router.add_get("/activities", service.history)
    app.router.add_get("/activities/{id}", service.activity)

    async def close(app):
        service.mongo.close()

    app.on_cleanup.append(close)
    web.run_app(app, host="0.0.0.0", port=8080, access_log=None)


if __name__ == "__main__":
    main()
