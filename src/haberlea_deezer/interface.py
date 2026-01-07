"""Deezer module interface for Haberlea.

This module provides the main interface for downloading music from Deezer,
supporting MP3 and FLAC formats with Blowfish decryption.
"""

import contextlib
import re
from enum import Enum, auto
from typing import Any
from urllib.parse import urlparse

from rich import print
from yarl import URL

from haberlea.plugins.base import ModuleBase
from haberlea.utils.exceptions import InvalidURLError, ModuleAuthError
from haberlea.utils.models import (
    AlbumInfo,
    ArtistInfo,
    CodecEnum,
    CodecOptions,
    CoverCompressionEnum,
    CoverInfo,
    CoverOptions,
    CreditsInfo,
    DownloadEnum,
    DownloadTypeEnum,
    ImageFileTypeEnum,
    LyricsInfo,
    MediaIdentification,
    ModuleController,
    ModuleInformation,
    ModuleModes,
    PlaylistInfo,
    QualityEnum,
    SearchResult,
    Tags,
    TrackDownloadInfo,
    TrackInfo,
)

from .deezer_api import DeezerApi

module_information = ModuleInformation(
    service_name="Deezer",
    module_supported_modes=(
        ModuleModes.download
        | ModuleModes.lyrics
        | ModuleModes.covers
        | ModuleModes.credits
    ),
    global_settings={
        "client_id": "447462",
        "client_secret": "a83bf7f38ad2f137e444727cfc3775cf",
        "bf_secret": "g4el58wc0zvf9na1",
        "track_url_key": "jo6aey6haid2Teih",
    },
    session_settings={"email": "", "password": "", "user_arl": ""},
    session_storage_variables=["arl"],
    netlocation_constant=["deezer", "dzr"],
    url_constants={
        "track": DownloadTypeEnum.track,
        "album": DownloadTypeEnum.album,
        "playlist": DownloadTypeEnum.playlist,
        "artist": DownloadTypeEnum.artist,
    },
    test_url="https://www.deezer.com/track/3135556",
)


class ImageType(Enum):
    """Deezer image types for CDN URLs."""

    cover = auto()
    artist = auto()
    playlist = auto()
    user = auto()
    misc = auto()
    talk = auto()


class ModuleInterface(ModuleBase):
    """Deezer module interface implementation.

    Handles authentication, metadata retrieval, and track downloading
    from the Deezer music streaming service.
    """

    def __init__(self, module_controller: ModuleController) -> None:
        """Initialize the Deezer module.

        Args:
            module_controller: Controller providing access to settings and resources.
        """
        super().__init__(module_controller)
        self.settings = module_controller.module_settings
        self.tsc = module_controller.temporary_settings_controller
        self.cover_options = module_controller.haberlea_options.default_cover_options
        self.disable_subscription_check = (
            module_controller.haberlea_options.disable_subscription_check
        )

        # Deezer doesn't support webp
        if self.cover_options.file_type is ImageFileTypeEnum.webp:
            self.cover_options = CoverOptions(
                file_type=ImageFileTypeEnum.jpg,
                resolution=self.cover_options.resolution,
                compression=self.cover_options.compression,
            )

        self.api = DeezerApi(
            client_id=self.settings["client_id"],
            client_secret=self.settings["client_secret"],
            bf_secret=self.settings["bf_secret"],
            track_url_key=self.settings.get("track_url_key", ""),
        )

        # Set ARL for auto-relogin when api_token expires
        arl = self.settings.get("user_arl", "")
        if arl:
            self.api._arl = arl
            # Set cookie for session
            self.api.session.cookie_jar.update_cookies(
                {"arl": arl}, URL("https://www.deezer.com")
            )

        self.quality_map: dict[QualityEnum, str] = {
            QualityEnum.MINIMUM: "MP3_128",
            QualityEnum.LOW: "MP3_128",
            QualityEnum.MEDIUM: "MP3_320",
            QualityEnum.HIGH: "MP3_320",
            QualityEnum.LOSSLESS: "FLAC",
            QualityEnum.HIFI: "FLAC",
        }

        self.compression_map: dict[CoverCompressionEnum, int] = {
            CoverCompressionEnum.high: 80,
            CoverCompressionEnum.low: 50,
        }

        self.quality_tier = module_controller.haberlea_options.quality_tier
        self.target_format = self.quality_map[self.quality_tier]

    async def close(self) -> None:
        """Close the module and release resources."""
        await self.api.close()

    async def login(self, email: str, password: str) -> None:
        """Authenticate with Deezer.

        Args:
            email: User email address.
            password: User password.

        Raises:
            ModuleAuthError: If authentication fails.
        """
        arl = self.settings.get("user_arl", "")

        if arl:
            try:
                await self.api.login_via_arl(arl)
            except ModuleAuthError:
                if email and password:
                    arl, _ = await self.api.login_via_email(email, password)
                else:
                    raise
        elif email and password:
            arl, _ = await self.api.login_via_email(email, password)
        else:
            raise ModuleAuthError(module_name="deezer")

        self.tsc.set("arl", arl)
        self._check_subscription()

    def _check_subscription(self) -> None:
        """Check if target format is available with current subscription."""
        if self.disable_subscription_check:
            return

        if self.target_format not in self.api.available_formats:
            available = ", ".join(self.api.available_formats)
            print(
                f"Deezer: {self.target_format} is not available with your "
                f"subscription. Available formats: {available}"
            )

    def custom_url_parse(self, url: str) -> MediaIdentification:
        """Parse a Deezer URL to extract media type and ID.

        Args:
            url: Deezer URL to parse.

        Returns:
            MediaIdentification with parsed info.

        Raises:
            InvalidURLError: If URL cannot be parsed.
        """
        parsed = urlparse(url)

        # Handle short links
        if parsed.hostname == "dzr.page.link":
            # Short links not supported in async context
            raise InvalidURLError(url, "Short links not supported")

        path_match = re.match(
            r"^/(?:[a-z]{2}/)?(track|album|artist|playlist)/(\d+)/?$",
            parsed.path,
        )

        if not path_match:
            raise InvalidURLError(url, "Invalid Deezer URL format")

        media_type = DownloadTypeEnum[path_match.group(1)]
        media_id = path_match.group(2)

        return MediaIdentification(
            media_type=media_type,
            media_id=media_id,
            original_url=url,
        )

    def _get_image_url(
        self,
        md5_hash: str,
        img_type: ImageType,
        file_type: ImageFileTypeEnum,
        resolution: int,
        compression: int,
    ) -> str:
        """Generate Deezer CDN image URL.

        Args:
            md5_hash: Image MD5 hash identifier.
            img_type: Type of image (cover, artist, etc.).
            file_type: Image file format.
            resolution: Image resolution in pixels.
            compression: JPEG compression quality.

        Returns:
            CDN image URL string.
        """
        if resolution > 3000:
            resolution = 3000

        if file_type == ImageFileTypeEnum.jpg:
            filename = f"{resolution}x0-000000-{compression}-0-0.jpg"
        else:
            filename = f"{resolution}x0-none-100-0-0.png"

        return (
            f"https://cdn-images.dzcdn.net/images/{img_type.name}/{md5_hash}/{filename}"
        )

    async def _get_track_data(
        self, track_id: str, data: dict[str, Any], is_user_uploaded: bool
    ) -> dict[str, Any]:
        """Get track data from cache or API.

        Args:
            track_id: Track identifier.
            data: Pre-fetched data dictionary.
            is_user_uploaded: Whether track is user-uploaded.

        Returns:
            Track data dictionary.
        """
        if track_id in data:
            track = data[track_id]
        elif not is_user_uploaded:
            track = await self.api.get_track(track_id)
        else:
            track = await self.api.get_track_data(track_id)

        t_data = track
        if not is_user_uploaded:
            t_data = t_data.get("DATA", t_data)
        if "FALLBACK" in t_data:
            t_data = t_data["FALLBACK"]

        return t_data

    def _build_track_tags(self, t_data: dict[str, Any]) -> Tags:
        """Build Tags object from track data.

        Args:
            t_data: Track data dictionary.

        Returns:
            Tags object with metadata.
        """
        return Tags(
            track_number=int(t_data.get("TRACK_NUMBER", 0)) or None,
            copyright=t_data.get("COPYRIGHT"),
            isrc=t_data.get("ISRC"),
            disc_number=int(t_data.get("DISK_NUMBER", 0)) or None,
            replay_gain=float(t_data["GAIN"]) if t_data.get("GAIN") else None,
            release_date=t_data.get("PHYSICAL_RELEASE_DATE"),
        )

    def _check_track_availability(
        self, t_data: dict[str, Any], audio_format: str, is_user_uploaded: bool
    ) -> tuple[str, str | None]:
        """Check track availability and determine format.

        Args:
            t_data: Track data dictionary.
            audio_format: Requested audio format.
            is_user_uploaded: Whether track is user-uploaded.

        Returns:
            Tuple of (final_format, error_message).
        """
        error: str | None = None

        if is_user_uploaded:
            rights = t_data.get("RIGHTS", {})
            if not rights.get("STREAM_ADS_AVAILABLE"):
                error = "Cannot download track uploaded by another user"
            return audio_format, error

        # Find best available format
        available_format = self._find_available_format(t_data, audio_format)
        final_format = available_format or "MP3_128"

        # Check country availability
        countries = t_data.get("AVAILABLE_COUNTRIES", {}).get("STREAM_ADS", [])
        if countries and self.api.country not in countries:
            error = f"Track not available in {self.api.country}"
        elif final_format not in self.api.available_formats:
            error = f"Format {final_format} not available with your subscription"

        return final_format, error

    def _get_track_artists(self, t_data: dict[str, Any]) -> list[str]:
        """Extract artist names from track data.

        Args:
            t_data: Track data dictionary.

        Returns:
            List of artist names.
        """
        if "ARTISTS" in t_data:
            return [a["ART_NAME"] for a in t_data["ARTISTS"]]
        return [t_data.get("ART_NAME", "Unknown")]

    def _calculate_track_codec_bitrate(
        self, audio_format: str
    ) -> tuple[CodecEnum, int | None]:
        """Determine codec and bitrate from audio format.

        Args:
            audio_format: Audio format string.

        Returns:
            Tuple of (codec, bitrate).
        """
        codec_map = {
            "MP3_MISC": CodecEnum.MP3,
            "MP3_128": CodecEnum.MP3,
            "MP3_320": CodecEnum.MP3,
            "FLAC": CodecEnum.FLAC,
        }
        codec = codec_map.get(audio_format, CodecEnum.MP3)

        bitrate_map = {
            "MP3_MISC": None,
            "MP3_128": 128,
            "MP3_320": 320,
            "FLAC": 1411,
        }
        bitrate = bitrate_map.get(audio_format)

        return codec, bitrate

    async def get_track_info(
        self,
        track_id: str,
        quality_tier: QualityEnum,
        codec_options: CodecOptions,
        data: dict[str, Any] | None = None,
    ) -> TrackInfo:
        """Get track information and metadata.

        Args:
            track_id: Track identifier.
            quality_tier: Desired audio quality.
            codec_options: Codec preference options.
            data: Optional pre-fetched track data.

        Returns:
            TrackInfo with metadata and download information.
        """
        if data is None:
            data = {}

        is_user_uploaded = int(track_id) < 0
        audio_format = (
            self.quality_map[quality_tier] if not is_user_uploaded else "MP3_MISC"
        )

        # Get track data
        t_data = await self._get_track_data(track_id, data, is_user_uploaded)

        # Build tags
        tags = self._build_track_tags(t_data)

        # Check availability and determine format
        audio_format, error = self._check_track_availability(
            t_data, audio_format, is_user_uploaded
        )

        # Map format to codec and bitrate
        codec, bitrate = self._calculate_track_codec_bitrate(audio_format)

        # Build track name
        track_name = t_data.get("SNG_TITLE", "")
        if t_data.get("VERSION"):
            track_name = f"{track_name} {t_data['VERSION']}"

        # Get artists
        artists = self._get_track_artists(t_data)

        # Cover URL
        cover_url = self._get_image_url(
            t_data.get("ALB_PICTURE", ""),
            ImageType.cover,
            ImageFileTypeEnum.jpg,
            self.cover_options.resolution,
            self.compression_map[self.cover_options.compression],
        )

        # Release year
        release_year: int = 0
        if tags.release_date:
            with contextlib.suppress(ValueError, IndexError):
                release_year = int(tags.release_date.split("-")[0])

        # Get original track data for lyrics
        track = await self.api.get_track(track_id) if not is_user_uploaded else {}

        return TrackInfo(
            name=track_name,
            album_id=str(t_data.get("ALB_ID", "")),
            album=t_data.get("ALB_TITLE", ""),
            artists=artists,
            tags=tags,
            codec=codec,
            cover_url=cover_url,
            release_year=release_year,
            explicit=t_data.get("EXPLICIT_LYRICS") == "1",
            artist_id=str(t_data.get("ART_ID", "")),
            bit_depth=16,
            sample_rate=44.1,
            bitrate=bitrate,
            download_data={
                "track_id": t_data["SNG_ID"],
                "track_token": t_data.get("TRACK_TOKEN", ""),
                "track_token_expiry": float(t_data.get("TRACK_TOKEN_EXPIRE", 0)),
                "format": audio_format,
            },
            cover_data={"md5": t_data.get("ALB_PICTURE", "")},
            credits_data={"contributors": t_data.get("SNG_CONTRIBUTORS")},
            lyrics_data={
                "lyrics": track.get("LYRICS") if not is_user_uploaded else None
            },
            error=error,
        )

    def _find_available_format(
        self, track_data: dict[str, Any], target_format: str
    ) -> str | None:
        """Find the best available format for a track.

        Args:
            track_data: Track data dictionary.
            target_format: Desired format.

        Returns:
            Best available format string, or None if none available.
        """
        formats_priority = ["FLAC", "MP3_320", "MP3_128"]

        # Start from target format
        try:
            start_idx = formats_priority.index(target_format)
        except ValueError:
            start_idx = 0

        for fmt in formats_priority[start_idx:]:
            filesize_key = f"FILESIZE_{fmt}"
            if track_data.get(filesize_key, "0") != "0":
                return fmt

        return None

    async def get_track_download(
        self,
        target_path: str,
        url: str = "",
        data: dict[str, Any] | None = None,
    ) -> TrackDownloadInfo:
        """Download and decrypt a track.

        Args:
            target_path: Target file path for the download.
            url: Unused for Deezer (URL is fetched dynamically).
            data: Download data containing track_id, track_token, etc.

        Returns:
            TrackDownloadInfo with download result.
        """
        if data is None:
            raise ValueError("Download data is required for Deezer tracks")

        track_id = data["track_id"]
        track_token = data["track_token"]
        track_token_expiry = data["track_token_expiry"]
        audio_format = data["format"]

        # Get streaming URL
        stream_url = await self.api.get_track_url(
            track_id, track_token, track_token_expiry, audio_format
        )

        # Download and decrypt directly to target path
        await self.api.download_and_decrypt_track(
            track_id, stream_url, target_path, self.api.session
        )

        return TrackDownloadInfo(download_type=DownloadEnum.DIRECT)

    async def get_album_info(
        self, album_id: str, data: dict[str, Any] | None = None
    ) -> AlbumInfo:
        """Get album information and track list.

        Args:
            album_id: Album identifier.
            data: Optional pre-fetched album data.

        Returns:
            AlbumInfo with metadata and track list.
        """
        if data is None:
            data = {}

        album = data.get(album_id) or await self.api.get_album(album_id)
        a_data = album["DATA"]

        # Get genres
        genres = await self.api.get_album_genres(album_id)

        # Determine cover type (placeholder images can't be PNG)
        cover_type = (
            self.cover_options.file_type
            if a_data.get("ALB_PICTURE")
            else ImageFileTypeEnum.jpg
        )

        # Get track list
        tracks_data = album.get("SONGS", {}).get("data", [])
        tracks = [str(t["SNG_ID"]) for t in tracks_data]

        # Calculate totals
        total_tracks = int(tracks_data[-1]["TRACK_NUMBER"]) if tracks_data else 0
        total_discs = int(tracks_data[-1]["DISK_NUMBER"]) if tracks_data else 0

        # Release date
        release_date = a_data.get("ORIGINAL_RELEASE_DATE") or a_data.get(
            "PHYSICAL_RELEASE_DATE"
        )
        release_year = int(release_date.split("-")[0]) if release_date else 0

        # Cover URL
        cover_url = self._get_image_url(
            a_data.get("ALB_PICTURE", ""),
            ImageType.cover,
            cover_type,
            self.cover_options.resolution,
            self.compression_map[self.cover_options.compression],
        )

        # Build track data for passing to get_track_info
        track_data: dict[str, Any] = {}
        album_tags = {
            "total_tracks": total_tracks,
            "total_discs": total_discs,
            "upc": a_data.get("UPC"),
            "label": a_data.get("LABEL_NAME"),
            "album_artist": a_data.get("ART_NAME"),
            "release_date": release_date,
            "genres": genres,
        }

        for t in tracks_data:
            tid = str(t["SNG_ID"])
            track_data[tid] = t
            track_data[tid]["_album_tags"] = album_tags

        return AlbumInfo(
            name=a_data.get("ALB_TITLE", ""),
            artist=a_data.get("ART_NAME", ""),
            tracks=tracks,
            release_year=release_year,
            explicit=a_data.get("EXPLICIT_ALBUM_CONTENT", {}).get(
                "EXPLICIT_LYRICS_STATUS", 0
            )
            in (1, 4),
            artist_id=str(a_data.get("ART_ID", "")),
            upc=a_data.get("UPC"),
            cover_url=cover_url,
            cover_type=cover_type,
            track_data=track_data,
        )

    async def get_playlist_info(
        self, playlist_id: str, data: dict[str, Any] | None = None
    ) -> PlaylistInfo:
        """Get playlist information and track list.

        Args:
            playlist_id: Playlist identifier.
            data: Optional pre-fetched playlist data.

        Returns:
            PlaylistInfo with metadata and track list.
        """
        if data is None:
            data = {}

        playlist = data.get(playlist_id) or await self.api.get_playlist(
            playlist_id, -1, 0
        )
        p_data = playlist["DATA"]

        # Determine cover type
        cover_type = (
            self.cover_options.file_type
            if p_data.get("PLAYLIST_PICTURE")
            else ImageFileTypeEnum.jpg
        )

        # Get tracks
        tracks_data = playlist.get("SONGS", {}).get("data", [])
        tracks = [str(t["SNG_ID"]) for t in tracks_data]

        # Build track data for user-uploaded tracks
        track_data: dict[str, Any] = {}
        for t in tracks_data:
            if int(t["SNG_ID"]) < 0:
                track_data[str(t["SNG_ID"])] = t

        # Cover URL
        cover_url = self._get_image_url(
            p_data.get("PLAYLIST_PICTURE", ""),
            ImageType.playlist,
            cover_type,
            self.cover_options.resolution,
            self.compression_map[self.cover_options.compression],
        )

        return PlaylistInfo(
            name=p_data.get("TITLE", ""),
            creator=p_data.get("PARENT_USERNAME", ""),
            tracks=tracks,
            release_year=int(p_data.get("DATE_ADD", "0000")[:4]),
            creator_id=str(p_data.get("PARENT_USER_ID", "")),
            cover_url=cover_url,
            cover_type=cover_type,
            description=p_data.get("DESCRIPTION"),
            track_data=track_data if track_data else None,
        )

    async def get_artist_info(
        self, artist_id: str, get_credited_albums: bool = False
    ) -> ArtistInfo:
        """Get artist information and discography.

        Args:
            artist_id: Artist identifier.
            get_credited_albums: Whether to include credited albums.

        Returns:
            ArtistInfo with metadata and album list.
        """
        name = await self.api.get_artist_name(artist_id)
        albums = await self.api.get_artist_album_ids(
            artist_id, 0, -1, get_credited_albums
        )

        return ArtistInfo(name=name, albums=albums)

    async def get_track_credits(
        self, track_id: str, data: dict[str, Any] | None = None
    ) -> list[CreditsInfo]:
        """Get track credits/contributors.

        Args:
            track_id: Track identifier.
            data: Optional pre-fetched credits data.

        Returns:
            List of CreditsInfo objects.
        """
        if int(track_id) < 0:
            return []

        if data is None:
            data = {}

        contributors = data.get("contributors")
        if contributors is None:
            contributors = await self.api.get_track_contributors(track_id)

        if not contributors:
            return []

        # Remove redundant artist credit
        contributors.pop("artist", None)

        return [CreditsInfo(type=k, names=v) for k, v in contributors.items()]

    async def get_track_cover(
        self,
        track_id: str,
        cover_options: CoverOptions,
        data: dict[str, Any] | None = None,
    ) -> CoverInfo:
        """Get track cover image.

        Args:
            track_id: Track identifier.
            cover_options: Cover image options.
            data: Optional pre-fetched cover data.

        Returns:
            CoverInfo with URL and file type.
        """
        if data is None:
            data = {}

        cover_md5 = data.get("md5")
        if cover_md5 is None:
            cover_md5 = await self.api.get_track_cover(track_id)

        # Placeholder images can't be PNG, and Deezer doesn't support webp
        file_type = cover_options.file_type
        if not cover_md5 or file_type == ImageFileTypeEnum.webp:
            file_type = ImageFileTypeEnum.jpg

        url = self._get_image_url(
            cover_md5,
            ImageType.cover,
            file_type,
            cover_options.resolution,
            self.compression_map[cover_options.compression],
        )

        return CoverInfo(url=url, file_type=file_type)

    async def get_track_lyrics(
        self, track_id: str, data: dict[str, Any] | None = None
    ) -> LyricsInfo:
        """Get track lyrics.

        Args:
            track_id: Track identifier.
            data: Optional pre-fetched lyrics data.

        Returns:
            LyricsInfo with embedded and synced lyrics.
        """
        if int(track_id) < 0:
            return LyricsInfo()

        if data is None:
            data = {}

        lyrics = data.get("lyrics")
        if lyrics is None:
            try:
                lyrics = await self.api.get_track_lyrics(track_id)
            except Exception:
                return LyricsInfo()

        if not lyrics:
            return LyricsInfo()

        # Build synced lyrics from JSON
        synced_text: str | None = None
        if "LYRICS_SYNC_JSON" in lyrics:
            lines: list[str] = []
            for line in lyrics["LYRICS_SYNC_JSON"]:
                if "lrc_timestamp" in line:
                    lines.append(f"{line['lrc_timestamp']}{line['line']}")
                else:
                    lines.append("")
            synced_text = "\n".join(lines)

        return LyricsInfo(
            embedded=lyrics.get("LYRICS_TEXT"),
            synced=synced_text,
        )

    async def search(
        self,
        query_type: DownloadTypeEnum,
        query: str,
        track_info: TrackInfo | None = None,
        limit: int = 10,
    ) -> list[SearchResult]:
        """Search for content on Deezer.

        Args:
            query_type: Type of content to search for.
            query: Search query string.
            track_info: Optional track info for ISRC-based search.
            limit: Maximum number of results.

        Returns:
            List of SearchResult objects.
        """
        results: list[dict[str, Any]] = []

        # Try ISRC search first for tracks
        if track_info and track_info.tags.isrc:
            try:
                isrc_result = await self.api.get_track_by_isrc(track_info.tags.isrc)
                results = [isrc_result]
            except Exception as e:
                print(f"ISRC search failed: {e}")

        if not results:
            search_data = await self.api.search(query, query_type.name, 0, limit)
            results = search_data.get("data", [])

        return self._format_search_results(query_type, results)

    def _format_search_results(
        self, query_type: DownloadTypeEnum, results: list[dict[str, Any]]
    ) -> list[SearchResult]:
        """Format raw search results into SearchResult objects.

        Args:
            query_type: Type of content searched.
            results: Raw search results.

        Returns:
            List of formatted SearchResult objects.
        """
        formatted: list[SearchResult] = []

        for item in results:
            if query_type == DownloadTypeEnum.track:
                name = item.get("SNG_TITLE", "")
                if item.get("VERSION"):
                    name = f"{name} {item['VERSION']}"

                artists = (
                    [a["ART_NAME"] for a in item.get("ARTISTS", [])]
                    if "ARTISTS" in item
                    else [item.get("ART_NAME", "")]
                )

                formatted.append(
                    SearchResult(
                        result_id=str(item.get("SNG_ID", "")),
                        name=name,
                        artists=artists,
                        explicit=item.get("EXPLICIT_LYRICS") == "1",
                        additional=[item.get("ALB_TITLE", "")],
                    )
                )

            elif query_type == DownloadTypeEnum.album:
                artists = (
                    [a["ART_NAME"] for a in item.get("ARTISTS", [])]
                    if "ARTISTS" in item
                    else [item.get("ART_NAME", "")]
                )

                release_date = item.get("PHYSICAL_RELEASE_DATE", "")
                year = release_date.split("-")[0] if release_date else None

                formatted.append(
                    SearchResult(
                        result_id=str(item.get("ALB_ID", "")),
                        name=item.get("ALB_TITLE", ""),
                        artists=artists,
                        year=year,
                        explicit=item.get("EXPLICIT_ALBUM_CONTENT", {}).get(
                            "EXPLICIT_LYRICS_STATUS", 0
                        )
                        in (1, 4),
                        additional=[str(item.get("NUMBER_TRACK", ""))],
                    )
                )

            elif query_type == DownloadTypeEnum.artist:
                formatted.append(
                    SearchResult(
                        result_id=str(item.get("ART_ID", "")),
                        name=item.get("ART_NAME", ""),
                    )
                )

            elif query_type == DownloadTypeEnum.playlist:
                formatted.append(
                    SearchResult(
                        result_id=str(item.get("PLAYLIST_ID", "")),
                        name=item.get("TITLE", ""),
                        artists=[item.get("PARENT_USERNAME", "")],
                        additional=[str(item.get("NB_SONG", ""))],
                    )
                )

        return formatted
