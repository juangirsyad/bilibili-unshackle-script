import re
from typing import Optional, Union
from http.cookiejar import CookieJar

import click
from langcodes import Language

from unshackle.core.credential import Credential
from unshackle.core.service import Service
from unshackle.core.titles import Episode, Movie, Movies, Series
from unshackle.core.tracks import Audio, Chapters, Chapter, Subtitle, Track, Video

class BILI(Service):
    """
    Service code for Bilibili International (https://bilibili.tv).

    \b
    Authorization: Cookies
    Security: No DRM
    Region: bilibili.tv (international)

    \b
    URL formats supported:
      Movie  : https://www.bilibili.tv/play/{season_id}
      Episode: https://www.bilibili.tv/play/{season_id}/{ep_id}
    """

    ALIASES = ["BILI", "bili", "bilibili"]
    # Matches:
    #   https://www.bilibili.tv/play/2412684              (movie / series root)
    #   https://www.bilibili.tv/play/2342380/27392045     (episode)
    #   optional language prefix: /en/, /th/, etc.
    TITLE_RE = (
        r"^(?:https?://(?:www\.)?bili(?:bili\.tv|intl\.com)/)"
        r"(?:[a-zA-Z]{2}/)?"
        r"(?:"
        r"(?:play|media)/(?P<season_id>\d+)(?:/(?P<ep_id>\d+))?"
        r"|video/(?P<aid>\d+)"
        r")"
    )

    @staticmethod
    @click.command(name="BILI", short_help="https://bilibili.tv")
    @click.argument("title", type=str, required=False)
    @click.pass_context
    def cli(ctx, **kwargs):
        return BILI(ctx, **kwargs)

    def __init__(self, ctx, title):
        super().__init__(ctx)
        self.title = title

    def authenticate(
        self,
        cookies: Optional[CookieJar] = None,
        credential: Optional[Credential] = None,
    ) -> None:
        super().authenticate(cookies, credential)
        if cookies:
            self.session.cookies.update(cookies)

        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.bilibili.tv/",
                "Origin": "https://www.bilibili.tv",
            }
        )

    def _call_api(self, url: str, video_id: str, **kwargs) -> dict:
        """Call a Bilibili TV API endpoint and raise on error codes."""
        resp = self.session.get(url, **kwargs).json()
        code = resp.get("code", 0)
        if code != 0:
            # 10004004 / 10004005 / 10023006 → login required
            # 10004001 → geo-restricted
            msg = resp.get("message", str(code))
            if code in (10004004, 10004005, 10023006):
                raise self.log.exit(
                    f"Content requires login (code {code}): {msg}"
                )
            elif code == 10004001:
                raise self.log.exit(
                    f"Content is geo-restricted in your region (code {code}): {msg}"
                )
            else:
                raise self.log.exit(f"API error (code {code}): {msg}")
        return resp.get("data") or {}

    def _get_season_info(self, season_id: str) -> dict:
        url = self.config["endpoints"]["season_info"].format(season_id)
        return self._call_api(url, season_id).get("season") or {}

    def _get_season_episodes(self, season_id: str) -> list[dict]:
        url = self.config["endpoints"]["season_episodes"].format(season_id)
        data = self._call_api(url, season_id)
        episodes = []
        for section in data.get("sections") or []:
            episodes.extend(section.get("episodes") or [])
        return episodes

    def _get_episode_info(self, ep_id: str) -> dict:
        url = self.config["endpoints"]["episode_info"].format(ep_id)
        return self._call_api(url, ep_id)

    def get_titles(self) -> Union[Movies, Series]:
        match = re.match(self.TITLE_RE, self.title)
        if not match:
            raise ValueError(
                "Could not parse ID from URL — is the URL correct?\n"
                "Supported formats:\n"
                "  https://www.bilibili.tv/play/{season_id}\n"
                "  https://www.bilibili.tv/play/{season_id}/{ep_id}\n"
            )

        season_id = match.group("season_id")
        ep_id = match.group("ep_id")
        aid = match.group("aid")

        self.log.debug(f"season_id={season_id!r}  ep_id={ep_id!r}  aid={aid!r}")

        if aid:
            ugc_url = self.config["endpoints"]["ugc_view"].format(aid)
            ugc_data = self._call_api(ugc_url, aid)
            archive = ugc_data.get("archive") or {}
            return Movies(
                [
                    Movie(
                        id_=aid,
                        service=self.__class__,
                        name=archive.get("title", aid),
                        year=None,
                        data={"aid": aid, "archive": archive},
                        language="und",
                    )
                ]
            )

        season_info = self._get_season_info(season_id)
        raw_season_title = season_info.get("title", season_id)
        season_type = season_info.get("type", 0)
        # type: 1 = movie, 2 = documentary, 4 = drama, 5 = variety …
        # Only type 1 is typically a single-episode "movie" on bilibili.tv
        is_movie = season_type == 1

        dub_lang = self._detect_dub_language(raw_season_title)
        season_title = self._strip_dub_suffix(raw_season_title)
        area_lang = self._detect_area_language(season_info)
        audio_lang = dub_lang or area_lang

        episodes_data = self._get_season_episodes(season_id)
        if not episodes_data:
            raise self.log.exit(f"No episodes found for season {season_id}")

        if ep_id:
            episodes_data = [
                ep for ep in episodes_data if str(ep.get("episode_id")) == ep_id
            ]
            if not episodes_data:
                raise self.log.exit(
                    f"Episode {ep_id} not found in season {season_id}"
                )

        if is_movie and not ep_id:
            ep = episodes_data[0]
            return Movies(
                [
                    Movie(
                        id_=str(ep["episode_id"]),
                        service=self.__class__,
                        name=season_title,
                        year=self._parse_year(season_info),
                        data={
                            "ep_id": str(ep["episode_id"]),
                            "season_id": season_id,
                            "ep_data": ep,
                            "audio_language": audio_lang,
                        },
                        language=audio_lang,
                    )
                ]
            )

        titles = []
        for ep in episodes_data:
            ep_raw_name = ep.get("long_title") or ep.get("title_display") or ep.get("title") or ""
            ep_number = ep.get("episode_number") or self._extract_ep_number(ep.get("title_display") or ep.get("title") or "")
            ep_season = ep.get("season_number") or 1
            ep_clean_name = self._clean_ep_name(ep_raw_name, season_title, raw_season_title)

            titles.append(
                Episode(
                    id_=str(ep["episode_id"]),
                    service=self.__class__,
                    title=season_title,
                    name=ep_clean_name,
                    season=int(ep_season),
                    number=int(ep_number) if ep_number else 0,
                    year=self._parse_year(season_info),
                    data={
                        "ep_id": str(ep["episode_id"]),
                        "season_id": season_id,
                        "ep_data": ep,
                        "audio_language": audio_lang,
                    },
                    language=audio_lang,
                )
            )
        return Series(titles)

    def get_tracks(self, title: Union[Movie, Episode]):
        ep_id: Optional[str] = title.data.get("ep_id")
        aid: Optional[str] = title.data.get("aid")

        params: dict = {"platform": "web"}
        if ep_id:
            params["ep_id"] = ep_id
        elif aid:
            params["aid"] = aid
        else:
            raise self.log.exit("Cannot determine ep_id or aid for this title")

        playurl_resp = self.session.get(
            self.config["endpoints"]["playurl"], params=params
        ).json()

        if playurl_resp.get("code", 0) != 0:
            msg = playurl_resp.get("message", "")
            code = playurl_resp.get("code")
            if code in (10004004, 10004005, 10023006):
                raise self.log.exit(
                    "This quality / content requires a premium account. "
                    "Please provide cookies from a logged-in premium account."
                )
            raise self.log.exit(f"playurl error ({code}): {msg}")

        playurl_data = (playurl_resp.get("data") or {}).get("playurl") or {}

        from unshackle.core.tracks import Tracks

        tracks = Tracks()

        for vid_stream in playurl_data.get("video") or []:
            res = vid_stream.get("video_resource") or {}
            info = vid_stream.get("stream_info") or {}
            url = res.get("url")
            if not url:
                continue

            backup_urls = res.get("backup_url") or []
            all_urls = [url] + backup_urls

            height = res.get("height") or 0
            width = res.get("width") or 0

            if height > 1080 and not self._is_2160p_requested():
                continue

            codec_str = (res.get("codecs") or "").lower()
            codec = (
                Video.Codec.AVC
                if "avc" in codec_str or "h264" in codec_str or "264" in codec_str
                else Video.Codec.HEVC
                if "hevc" in codec_str or "h265" in codec_str or "265" in codec_str
                else Video.Codec.AVC
            )

            tracks.add(
                Video(
                    id_=f"v{res.get('id', height)}",
                    url=all_urls[0],
                    codec=codec,
                    language="und",
                    bitrate=res.get("bandwidth"),
                    width=width,
                    height=height,
                    data={
                        "stream_info": info,
                        "resource": res,
                        "backup_urls": backup_urls,
                    },
                    descriptor=Track.Descriptor.URL,
                )
            )

        for aud in playurl_data.get("audio_resource") or []:
            url = aud.get("url")
            if not url:
                continue
            codec_str = (aud.get("codecs") or "").lower()
            codec = (
                Audio.Codec.AAC
                if "aac" in codec_str
                else Audio.Codec.EAC3
                if "ec-3" in codec_str or "eac3" in codec_str
                else Audio.Codec.AAC
            )
            tracks.add(
                Audio(
                    id_=f"a{aud.get('id', aud.get('bandwidth', 0))}",
                    url=url,
                    codec=codec,
                    language=title.data.get("audio_language") or title.language,
                    bitrate=aud.get("bandwidth"),
                    descriptive=False,
                    data={"resource": aud},
                    descriptor=Track.Descriptor.URL,
                )
            )

        sub_params: dict = {"platform": "web", "s_locale": "en_US"}
        if ep_id:
            sub_params["episode_id"] = ep_id
        elif aid:
            sub_params["aid"] = aid

        sub_resp = self.session.get(
            self.config["endpoints"]["subtitle"], params=sub_params
        ).json()
        sub_data = (sub_resp.get("data") or {})

        # Prefer 'subtitles' list; fall back to 'video_subtitle' if empty.
        # Both keys can carry the same languages — using only one avoids duplicates.
        sub_list = sub_data.get("subtitles") or sub_data.get("video_subtitle") or []

        seen_lang_ext: set[tuple] = set()
        for sub in sub_list:
            lang_key = sub.get("lang_key") or "und"
            try:
                lang_obj = Language.get(lang_key)
            except Exception:
                lang_obj = Language.get("und")

            for url in self._iter_subtitle_urls(sub):
                ext = url.rsplit(".", 1)[-1].lower()
                if ext not in ("ass", "srt", "vtt"):
                    ext = "srt"

                # Skip if we already have this language+format combination
                key = (lang_key, ext)
                if key in seen_lang_ext:
                    continue
                seen_lang_ext.add(key)

                tracks.add(
                    Subtitle(
                        id_=f"sub-{lang_key}-{ext}",
                        url=url,
                        codec=Subtitle.Codec.SubRip
                        if ext == "srt"
                        else Subtitle.Codec.ASS
                        if ext == "ass"
                        else Subtitle.Codec.WebVTT,
                        language=lang_obj,
                        is_original_lang=False,
                        forced=False,
                        sdh=False,
                        name=lang_obj.display_name(),
                        descriptor=Track.Descriptor.URL,
                        data={"sub_meta": sub},
                    )
                )

        return tracks

    def get_chapters(self, title: Union[Movie, Episode]) -> Chapters:
        ep_id: Optional[str] = title.data.get("ep_id")
        if not ep_id:
            return Chapters()

        ep_info = self._get_episode_info(ep_id)
        skip = ep_info.get("skip") or {}
        if not skip:
            return Chapters()

        chapters = []

        opening_start = self._ms_to_sec(skip.get("opening_start_time"))
        opening_end = self._ms_to_sec(skip.get("opening_end_time"))
        ending_start = self._ms_to_sec(skip.get("ending_start_time"))
        ending_end = self._ms_to_sec(skip.get("ending_end_time"))

        # Build chapter list from skip markers
        chapters.append(Chapter(timestamp=0))

        if opening_start is not None:
            chapters.append(Chapter(name="Intro", timestamp=opening_start))
        if opening_end is not None:
            chapters.append(Chapter(timestamp=opening_end))
        if ending_start is not None:
            chapters.append(Chapter(name="Credits", timestamp=ending_start))

        seen: set[float] = set()
        unique: list[Chapter] = []
        for ch in sorted(chapters, key=lambda c: c.timestamp):
            if ch.timestamp not in seen:
                seen.add(ch.timestamp)
                unique.append(ch)

        return Chapters(unique)

    def get_widevine_license(self, *, challenge: bytes, title, track) -> None:
        return None

    @staticmethod
    def _parse_year(season_info: dict) -> Optional[int]:
        """Try to extract a 4-digit year from season metadata."""
        pub = season_info.get("pub_time") or season_info.get("release_date") or ""
        m = re.search(r"(\d{4})", pub)
        return int(m.group(1)) if m else None

    @staticmethod
    def _extract_ep_number(title_display: str) -> Optional[int]:
        """Extract episode number from strings like 'E3 - Title' or 'Episode 3'."""
        m = re.search(r"(?:^E|Episode\s*)(\d+)", title_display, re.IGNORECASE)
        return int(m.group(1)) if m else None

    @staticmethod
    def _ms_to_sec(ms) -> Optional[float]:
        """Convert milliseconds to seconds, returns None if input is falsy."""
        if ms is None:
            return None
        try:
            return float(ms) / 1000.0
        except (TypeError, ValueError):
            return None

    def _is_2160p_requested(self) -> bool:
        """Check if 2160p should be included (either --list mode is active, or -q requested 2160p/4K)."""
        ctx = self.ctx
        while ctx:
            params = getattr(ctx, "params", {}) or {}

            for key, value in params.items():
                if "list" in key.lower() and value:
                    return True

            for key, value in params.items():
                if ("quality" in key.lower() or key.lower() in ("q", "res", "resolution")) and value:
                    quality = str(value).lower()
                    if "2160" in quality or "4k" in quality or "uhd" in quality:
                        return True

            ctx = getattr(ctx, "parent", None)
        return False

    _DUB_LANG_MAP: dict[str, str] = {
        "indo": "id", "indonesia": "id", "indonesian": "id", "id": "id",
        "jp": "ja", "japan": "ja", "japanese": "ja", "ja": "ja",
        "kr": "ko", "korea": "ko", "korean": "ko", "ko": "ko",
        "th": "th", "thai": "th", "thailand": "th",
        "cn": "zh", "china": "zh", "chinese": "zh", "zh": "zh",
        "en": "en", "eng": "en", "english": "en",
        "ms": "ms", "malay": "ms", "melayu": "ms",
        "vi": "vi", "viet": "vi", "vietnamese": "vi",
        "ar": "ar", "arabic": "ar",
        "es": "es", "spanish": "es",
        "pt": "pt", "portuguese": "pt",
        "fr": "fr", "french": "fr",
        "de": "de", "german": "de",
        "tr": "tr", "turkish": "tr",
    }

    @classmethod
    def _detect_dub_language(cls, title: str) -> Optional[str]:
        """Return a BCP-47 language code if the title contains a dub indicator.

        Handles patterns like:
          '(Dub Indo)', '（Dub Indo）', '[Dub Korean]', 'Dub Thai', etc.
        """
        # Bracketed match: supports ASCII (), [], {} and CJK full-width （）, 【】, ［］, 〔〕
        m = re.search(
            r"[\(\[\{\（\【\［\〔]\s*(?:dub|dubbed|audio)?\s*([a-zA-Z]+)\s*(?:dub|dubbed|audio)?\s*[\)\]\}\）\】\］\〕]",
            title, re.IGNORECASE,
        )
        if m:
            kw = m.group(1).lower()
            if kw in cls._DUB_LANG_MAP:
                return cls._DUB_LANG_MAP[kw]

        # Standalone "Dub <Lang>" or "<Lang> Dub"
        m = re.search(r"\b(?:dub|dubbed)\s+([a-zA-Z]+)\b|\b([a-zA-Z]+)\s+(?:dub|dubbed)\b", title, re.IGNORECASE)
        if m:
            kw = (m.group(1) or m.group(2)).lower()
            if kw in cls._DUB_LANG_MAP:
                return cls._DUB_LANG_MAP[kw]

        return None

    @classmethod
    def _detect_area_language(cls, season_info: dict) -> str:
        """Extract language from season_info area metadata, defaulting to Japanese for anime."""
        areas = season_info.get("area") or []
        for a in areas:
            if isinstance(a, dict):
                lang_key = a.get("lang_key")
                if lang_key:
                    return lang_key
                name = (a.get("name") or "").lower()
                if "japan" in name or "jp" in name:
                    return "ja"
                elif "china" in name or "cn" in name:
                    return "zh"
                elif "korea" in name or "kr" in name:
                    return "ko"
                elif "thailand" in name or "thai" in name:
                    return "th"
                elif "indonesia" in name or "indo" in name:
                    return "id"
        return "ja"

    @classmethod
    def _strip_dub_suffix(cls, title: str) -> str:
        """Remove dub/sub language suffixes from a season title.

        Examples:
          'MARRIAGETOXIN (Dub Indo)'  → 'MARRIAGETOXIN'
          'MARRIAGETOXIN（Dub Indo）' → 'MARRIAGETOXIN'
        """
        cleaned = re.sub(
            r"\s*[\(\[\{\（\【\［\〔][^\)\]\}\）\】\］\〕]*(?:dub|sub|dubbed|subbed|indo|thai|japanese|korean|english)[^\)\]\}\）\】\］\〕]*[\)\]\}\）\】\］\〕]",
            "", title, flags=re.IGNORECASE,
        ).strip()
        return cleaned or title

    @classmethod
    def _clean_ep_name(cls, raw_name: str, season_title: str, raw_season_title: str) -> str:
        """Clean episode display title to prevent duplication with season title / episode number."""
        name = raw_name or ""
        if raw_season_title:
            name = re.sub(re.escape(raw_season_title), "", name, flags=re.IGNORECASE)
        if season_title:
            name = re.sub(re.escape(season_title), "", name, flags=re.IGNORECASE)

        name = re.sub(
            r"\s*[\(\[\{\（\【\［\〔][^\)\]\}\）\】\］\〕]*(?:dub|sub|dubbed|subbed|indo|thai|japanese|korean|english)[^\)\]\}\）\】\］\〕]*[\)\]\}\）\】\］\〕]",
            "", name, flags=re.IGNORECASE,
        )

        # Remove leading/trailing E1, E01, Ep 1, Episode 1, etc.
        name = re.sub(r"^(?:E|Ep|Episode)?\s*\d+\s*[:\-\u2013]?\s*", "", name, flags=re.IGNORECASE).strip()

        # stripped everything away).
        return name or season_title

    @staticmethod
    def _iter_subtitle_urls(sub: dict):
        """Yield all URL variants from a subtitle dict (direct, .ass, .srt)."""
        for key in (None, "ass", "srt"):
            src = sub if key is None else sub.get(key) or {}
            url = src.get("url") if isinstance(src, dict) else None
            if url:
                yield url