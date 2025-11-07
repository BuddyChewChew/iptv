import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytz


class Time(datetime):
    ZONES = {
        "ET": pytz.timezone("America/New_York"),
        "PST": pytz.timezone("America/Los_Angeles"),
        "UTC": timezone.utc,
    }

    ZONES["EDT"] = ZONES["EST"] = ZONES["ET"]

    TZ = ZONES["ET"]

    @classmethod
    def now(cls) -> "Time":
        return cls.from_ts(datetime.now(cls.TZ).timestamp())

    @classmethod
    def from_ts(cls, ts: int | float) -> "Time":
        return cls.fromtimestamp(ts, tz=cls.TZ)

    @classmethod
    def default_8(cls) -> float:
        return (
            cls.now()
            .replace(hour=8, minute=0, second=0, microsecond=0, tzinfo=cls.TZ)
            .timestamp()
        )

    def delta(self, **kwargs) -> "Time":
        return self.from_ts((self + timedelta(**kwargs)).timestamp())

    def clean(self) -> "Time":
        return self.__class__.fromtimestamp(
            self.replace(second=0, microsecond=0).timestamp(),
            tz=self.TZ,
        )

    def to_tz(self, tzone: str) -> "Time":
        dt = self.astimezone(self.ZONES[tzone])
        return self.__class__.fromtimestamp(dt.timestamp(), tz=self.ZONES[tzone])

    @classmethod
    def from_str(
        cls,
        s: str,
        fmt: str | None = None,
    ) -> "Time":

        pattern = re.compile(r"\b(ET|UTC|EST|EDT|PST)\b")

        match = pattern.search(s)

        tz = cls.ZONES.get(match[1]) if match else cls.TZ

        cleaned_str = pattern.sub("", s).strip()

        if fmt:
            dt = datetime.strptime(cleaned_str, fmt)

        else:
            formats = [
                "%Y-%m-%d %H:%M",
                "%Y-%m-%d %H:%M:%S",
                "%Y/%m/%d %H:%M",
                "%Y/%m/%d %H:%M:%S",
                "%m/%d/%Y %H:%M",
                "%m/%d/%Y %H:%M:%S",
                "%m/%d/%Y %I:%M %p",
                "%Y/%m/%dT%H:%M:%S.%fZ",
                "%a, %d %b %Y %H:%M:%S %z",
            ]

            for frmt in formats:
                try:
                    dt = datetime.strptime(cleaned_str, frmt)
                    break
                except ValueError:
                    continue
            else:
                return cls.from_ts(Time.default_8())

        if not dt.tzinfo:
            dt = tz.localize(dt) if hasattr(tz, "localize") else dt.replace(tzinfo=tz)

        return cls.fromtimestamp(dt.astimezone(cls.TZ).timestamp(), tz=cls.TZ)


class Leagues:
    live_img = "https://i.gyazo.com/978f2eb4a199ca5b56b447aded0cb9e3.png"

    def __init__(self) -> None:
        self.data = json.loads(
            (Path(__file__).parent / "leagues.json").read_text(encoding="utf-8")
        )

    def teams(self, league: str) -> list[str]:
        return self.data["teams"].get(league, [])

    def info(self, name: str) -> tuple[str | None, str]:
        name = name.upper()

        if match := next(
            (
                (tvg_id, league_data.get("logo"))
                for tvg_id, leagues in self.data["leagues"].items()
                for league_entry in leagues
                for league_name, league_data in league_entry.items()
                if name == league_name or name in league_data.get("names", [])
            ),
            None,
        ):
            tvg_id, logo = match

            return (tvg_id, logo or self.live_img)

        return (None, self.live_img)

    def is_valid(
        self,
        event: str,
        league: str,
    ) -> bool:

        pattern = re.compile(r"\s+(?:-|vs\.?|at)\s+", flags=re.IGNORECASE)

        if pattern.search(event):
            t1, t2 = re.split(pattern, event)

            return any(t in self.teams(league) for t in (t1.strip(), t2.strip()))

        return event.lower() in {"nfl redzone", "college gameday"}

    def get_tvg_info(
        self,
        sport: str,
        event: str,
    ) -> tuple[str | None, str]:

        match sport:
            case "American Football" | "NFL":
                return (
                    self.info("NFL")
                    if self.is_valid(event, "NFL")
                    else self.info("NCAA")
                )

            case "Basketball" | "NBA":
                if self.is_valid(event, "NBA"):
                    return self.info("NBA")

                elif self.is_valid(event, "WNBA"):
                    return self.info("WNBA")

                # NCAA

                else:
                    return self.info("Basketball")

            case "Ice Hockey" | "Hockey":
                return self.info("NHL")

            case _:
                return self.info(sport)


leagues = Leagues()

__all__ = ["leagues", "Time"]
