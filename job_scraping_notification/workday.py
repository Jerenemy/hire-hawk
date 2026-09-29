"""Read Workday's public CXS endpoints; no browser or authentication required."""
from dataclasses import asdict, dataclass
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit, urlencode
from urllib.request import Request, urlopen

LOCATIONS = [
    "be264fa6cb8b011cf4589e6d18ba1127", "be264fa6cb8b01c1fd5b656c18bae126",
    "be264fa6cb8b01d075134b6c18bad126", "be264fa6cb8b013345813d6c18bac926",
    "be264fa6cb8b01f22a80586c18bad926", "be264fa6cb8b01fe8dc4006c18bab126",
]
DEFAULT_URL = (
    "https://spencerstuart.wd5.myworkdayjobs.com/en-US/Spencer_Stuart_External_Careers/jobs?"
    + urlencode({
        "locations": LOCATIONS,
        "workerSubType": ["be264fa6cb8b0175451b3a2012baaf10"],
        "timeType": ["d665eb2b24fc01b581cff464a3b96100"],
        "jobFamilyGroup": ["be264fa6cb8b01d203f2b5eb17ba5222"],
    }, doseq=True)
)


class FetchError(RuntimeError):
    """No complete, trustworthy snapshot could be obtained."""


@dataclass(frozen=True)
class Job:
    id: str
    title: str
    location: str
    requisition: str
    url: str
    posted_date: str | None = None
    posted_label: str | None = None

    def to_dict(self):
        return asdict(self)


def request_json(url, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = Request(url, data=data, headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": "SpencerJobNotifier/0.1",
    })
    for attempt in range(3):
        try:
            with urlopen(request, timeout=30) as response:
                result = json.load(response)
            if not isinstance(result, dict):
                raise FetchError("Workday returned a non-object response")
            return result
        except HTTPError as exc:
            if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                raise FetchError(f"Workday HTTP error {exc.code}") from exc
        except (URLError, TimeoutError, ConnectionError) as exc:
            if attempt == 2:
                raise FetchError("Workday unavailable after three attempts") from exc
        except (ValueError, UnicodeError) as exc:
            raise FetchError("Workday returned invalid JSON") from exc
        time.sleep(2 ** attempt)
    raise FetchError("Workday request failed")


class Workday:
    def __init__(self, search_url=DEFAULT_URL, request=request_json):
        parsed = urlsplit(search_url)
        parts = parsed.path.strip("/").split("/")
        if (parsed.scheme != "https" or parsed.hostname != "spencerstuart.wd5.myworkdayjobs.com"
                or parsed.port not in (None, 443) or parsed.username or parsed.password
                or parts != ["en-US", "Spencer_Stuart_External_Careers", "jobs"]
                or parsed.fragment):
            raise ValueError("SEARCH_URL must be the Spencer Stuart en-US Workday jobs search")
        query = parse_qs(parsed.query, keep_blank_values=True)
        allowed = {"locations", "workerSubType", "timeType", "jobFamilyGroup", "searchText"}
        if set(query) - allowed or any(not value for values in query.values() for value in values):
            raise ValueError("SEARCH_URL contains unsupported or empty filters")
        if len(query.get("searchText", [])) > 1:
            raise ValueError("SEARCH_URL must contain at most one searchText")
        self.search_text = query.pop("searchText", [""])[0]
        self.facets = query
        self.search_url = search_url
        self.origin = f"https://{parsed.hostname}"
        self.base = self.origin + "/wday/cxs/spencerstuart/Spencer_Stuart_External_Careers"
        self.request = request

    def fetch(self):
        rows = []
        paths = set()
        total = None
        while total is None or len(rows) < total:
            page = self.request(self.base + "/jobs", {
                "appliedFacets": self.facets, "limit": 20,
                "offset": len(rows), "searchText": self.search_text,
            })
            count, postings = page.get("total"), page.get("jobPostings")
            if type(count) is not int or count < 0 or not isinstance(postings, list):
                raise FetchError("Malformed listings response")
            if total is not None and count != total:
                raise FetchError("Listing count changed during pagination; retry next run")
            total = count
            if len(rows) + len(postings) > total or (not postings and len(rows) < total):
                raise FetchError("Incomplete or inconsistent pagination")
            for row in postings:
                path = row.get("externalPath") if isinstance(row, dict) else None
                if (not isinstance(path, str) or not path.startswith("/job/")
                        or "?" in path or "#" in path or "/../" in path or path in paths):
                    raise FetchError("Invalid or duplicate posting path")
                paths.add(path)
                rows.append(row)
        jobs = []
        ids = set()
        for row in rows:
            detail = self.request(self.base + row["externalPath"]).get("jobPostingInfo")
            if not isinstance(detail, dict):
                raise FetchError("Missing posting details")
            required = ("id", "title", "location", "jobReqId")
            if any(not isinstance(detail.get(k), str) or not detail[k].strip() for k in required):
                raise FetchError("Posting details missing required fields")
            if detail["id"] in ids or detail.get("posted") is not True:
                raise FetchError("Duplicate or withdrawn posting during fetch")
            ids.add(detail["id"])
            locations = [detail["location"]]
            additional = detail.get("additionalLocations", [])
            if not isinstance(additional, list) or any(not isinstance(x, str) for x in additional):
                raise FetchError("Invalid additional locations")
            locations.extend(additional)
            jobs.append(Job(detail["id"], detail["title"], "; ".join(dict.fromkeys(locations)),
                            detail["jobReqId"], self.origin +
                            "/en-US/Spencer_Stuart_External_Careers" + row["externalPath"],
                            posted_date=detail.get("startDate"),
                            posted_label=detail.get("postedOn") or row.get("postedOn")))
        return jobs
