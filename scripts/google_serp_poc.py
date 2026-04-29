#!/usr/bin/env python3
"""Google SERP HTML collection and paid-response comparison PoC."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class SerpLink:
    position: int
    title: str
    link: str
    display_link: str


class GoogleAnchorParser(HTMLParser):
    """Collect anchors and visible text while keeping the parser small."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[dict[str, str]] = []
        self._current_href = ""
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attrs_map = {key: value for key, value in attrs}
        href = attrs_map.get("href")
        if href is None:
            return
        self._current_href = href
        self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href == "":
            return
        self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a":
            return
        if self._current_href == "":
            return
        text = normalize_text(" ".join(self._current_text))
        self.anchors.append({"href": self._current_href, "text": text})
        self._current_href = ""
        self._current_text = []


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value)).strip()


def normalize_url(value: str) -> str:
    parsed = urlparse(value)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path
    if path != "/":
        path = path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


def display_link(value: str) -> str:
    parsed = urlparse(value)
    host = parsed.netloc.lower()
    return host.removeprefix("www.")


def extract_google_target(href: str) -> str:
    if href.startswith("/url?"):
        parsed = urlparse(href)
        values = parse_qs(parsed.query).get("q")
        if values is None:
            return ""
        if len(values) == 0:
            return ""
        return values[0]
    if href.startswith("http://") or href.startswith("https://"):
        parsed = urlparse(href)
        if parsed.netloc.endswith("google.com"):
            return ""
        return href
    return ""


def is_probable_organic_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return False
    blocked_hosts = {
        "accounts.google.com",
        "maps.google.com",
        "policies.google.com",
        "support.google.com",
        "webcache.googleusercontent.com",
    }
    host = parsed.netloc.lower()
    if host in blocked_hosts:
        return False
    return "google." not in host


def parse_google_html(html: str) -> list[SerpLink]:
    parser = GoogleAnchorParser()
    parser.feed(html)

    results: list[SerpLink] = []
    seen: set[str] = set()
    for anchor in parser.anchors:
        target = extract_google_target(anchor["href"])
        if not is_probable_organic_url(target):
            continue
        normalized = normalize_url(target)
        if normalized in seen:
            continue
        text = anchor["text"]
        if text == "":
            continue
        if len(text) > 180:
            text = text[:177] + "..."
        seen.add(normalized)
        results.append(
            SerpLink(
                position=len(results) + 1,
                title=text,
                link=target,
                display_link=display_link(target),
            )
        )
    return results


def fetch_google_html(query: str, hl: str, gl: str, num: int, timeout: int) -> tuple[str, int, str]:
    params = {
        "q": query,
        "num": str(num),
        "hl": hl,
        "gl": gl.lower(),
        "pws": "0",
        "gbv": "1",
        "sourceid": "chrome",
        "ie": "UTF-8",
    }
    url = "https://www.google.com/search?" + urlencode(params, quote_via=quote_plus)
    request = Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": f"{hl},zh;q=0.9,en;q=0.8",
            "User-Agent": DEFAULT_USER_AGENT,
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
            charset = response.headers.get_content_charset()
            if charset is None:
                charset = "utf-8"
            return url, response.status, body.decode(charset, errors="replace")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return url, exc.code, body
    except URLError as exc:
        raise RuntimeError(f"Google request failed: {exc}") from exc


def load_reference(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError("reference JSON must be an object")
    return data


def reference_organic(data: dict[str, Any]) -> list[dict[str, Any]]:
    value = data.get("organic")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def compare_with_reference(parsed: list[SerpLink], reference_data: dict[str, Any]) -> dict[str, Any]:
    reference_items = reference_organic(reference_data)
    parsed_by_url = {normalize_url(item.link): item for item in parsed}
    reference_by_url: dict[str, dict[str, Any]] = {}
    for item in reference_items:
        link = item.get("link")
        if isinstance(link, str):
            reference_by_url[normalize_url(link)] = item

    matched_urls = sorted(set(parsed_by_url).intersection(reference_by_url))
    missing_from_parsed = sorted(set(reference_by_url).difference(parsed_by_url))
    extra_in_parsed = sorted(set(parsed_by_url).difference(reference_by_url))

    return {
        "reference_organic_count": len(reference_by_url),
        "parsed_organic_count": len(parsed_by_url),
        "matched_count": len(matched_urls),
        "coverage_ratio": round(len(matched_urls) / len(reference_by_url), 4)
        if len(reference_by_url) > 0
        else None,
        "matched": [
            {
                "url": url,
                "parsed_position": parsed_by_url[url].position,
                "reference_position": reference_by_url[url].get("position"),
                "parsed_title": parsed_by_url[url].title,
                "reference_title": reference_by_url[url].get("title"),
            }
            for url in matched_urls
        ],
        "missing_from_parsed": [
            {
                "url": url,
                "reference_position": reference_by_url[url].get("position"),
                "reference_title": reference_by_url[url].get("title"),
            }
            for url in missing_from_parsed
        ],
        "extra_in_parsed": [
            {
                "url": url,
                "parsed_position": parsed_by_url[url].position,
                "parsed_title": parsed_by_url[url].title,
            }
            for url in extra_in_parsed
        ],
    }


def summarize_reference(reference_data: dict[str, Any]) -> dict[str, Any]:
    organic = reference_organic(reference_data)
    knowledge = reference_data.get("knowledge")
    related = reference_data.get("related")
    search_information = reference_data.get("search_information")
    spider_parameter = reference_data.get("spider_parameter")
    return {
        "organic_count": len(organic),
        "has_knowledge": isinstance(knowledge, dict),
        "related_count": len(related) if isinstance(related, list) else 0,
        "search_information": search_information if isinstance(search_information, dict) else {},
        "spider_parameter": spider_parameter if isinstance(spider_parameter, dict) else {},
        "organic_urls": [
            item.get("link")
            for item in organic
            if isinstance(item.get("link"), str)
        ],
    }


def detect_serp_state(html: str, status_code: int) -> dict[str, Any]:
    lower = html.lower()
    return {
        "status_code": status_code,
        "html_bytes": len(html.encode("utf-8")),
        "looks_like_consent": "consent.google.com" in lower or "before you continue" in lower,
        "looks_like_captcha": "/sorry/" in lower or "unusual traffic" in lower,
        "looks_like_enablejs": "/httpservice/retry/enablejs" in lower,
        "contains_search_form": "<form" in lower and "search" in lower,
        "contains_result_link_pattern": "/url?q=" in html,
    }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")


def write_csv(path: Path, rows: list[SerpLink]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["position", "title", "link", "display_link"])
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="阿里巴巴")
    parser.add_argument("--hl", default="zh-CN")
    parser.add_argument("--gl", default="hk")
    parser.add_argument("--num", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--reference-json", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("data/runs"))
    parser.add_argument(
        "--html-file",
        type=Path,
        help="Parse an existing HTML file instead of requesting Google.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = args.output_dir / timestamp
    raw_dir = run_dir / "raw"
    parsed_dir = run_dir / "parsed"
    reports_dir = run_dir / "reports"

    if args.html_file is None:
        search_url, status_code, html = fetch_google_html(
            query=args.query,
            hl=args.hl,
            gl=args.gl,
            num=args.num,
            timeout=args.timeout,
        )
    else:
        search_url = str(args.html_file)
        status_code = 0
        html = args.html_file.read_text(encoding="utf-8", errors="replace")

    raw_dir.mkdir(parents=True, exist_ok=True)
    html_path = raw_dir / "google.html"
    html_path.write_text(html, encoding="utf-8")

    results = parse_google_html(html)
    state = detect_serp_state(html, status_code)
    parsed_payload = {
        "query": args.query,
        "hl": args.hl,
        "gl": args.gl,
        "num": args.num,
        "search_url": search_url,
        "run_dir": str(run_dir),
        "serp_state": state,
        "organic": [asdict(item) for item in results],
    }
    write_json(parsed_dir / "google_parsed.json", parsed_payload)
    write_csv(parsed_dir / "google_organic.csv", results)

    comparison_path = None
    if args.reference_json is not None:
        reference_data = load_reference(args.reference_json)
        write_json(reports_dir / "reference_summary.json", summarize_reference(reference_data))
        comparison = compare_with_reference(results, reference_data)
        comparison["reference_json"] = str(args.reference_json)
        comparison["query"] = args.query
        write_json(reports_dir / "reference_comparison.json", comparison)
        comparison_path = reports_dir / "reference_comparison.json"

    print(f"run_dir={run_dir}")
    print(f"html={html_path}")
    print(f"parsed={parsed_dir / 'google_parsed.json'}")
    print(f"organic_count={len(results)}")
    print(f"status_code={status_code}")
    print(f"captcha={state['looks_like_captcha']}")
    print(f"consent={state['looks_like_consent']}")
    print(f"enablejs={state['looks_like_enablejs']}")
    if comparison_path is not None:
        print(f"comparison={comparison_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
