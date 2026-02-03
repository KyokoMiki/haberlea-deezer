"""Deezer API client for authentication and data retrieval.

This module provides async API access to Deezer's music streaming service,
handling authentication, track metadata, and encrypted stream downloads.
"""

from collections.abc import Callable
from hashlib import md5
from math import ceil
from random import randint
from time import time
from typing import Any

import aiohttp
import msgspec
from Cryptodome.Cipher import Blowfish
from Cryptodome.Hash import MD5
from yarl import URL

from haberlea.utils.exceptions import ModuleAPIError, ModuleAuthError
from haberlea.utils.utils import create_aiohttp_session, download_file


class DeezerApiError(msgspec.Struct):
    """Deezer API error response structure."""

    error_type: str
    message: str
    payload: dict[str, Any] | None = None


class DeezerApi:
    """Async Deezer API client.

    Handles authentication via ARL cookie or email/password,
    and provides access to track, album, playlist, and artist data.
    """

    GW_LIGHT_URL = "https://www.deezer.com/ajax/gw-light.php"
    MEDIA_URL = "https://media.deezer.com/v1/get_url"
    API_URL = "https://api.deezer.com"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        bf_secret: str,
        track_url_key: str = "",
    ) -> None:
        """Initialize the Deezer API client.

        Args:
            client_id: Deezer OAuth client ID.
            client_secret: Deezer OAuth client secret.
            bf_secret: Blowfish encryption secret for track decryption.
            track_url_key: Optional track URL key for legacy URLs.
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.bf_secret = bf_secret.encode("ascii")
        self.track_url_key = track_url_key

        self.session = create_aiohttp_session()
        self.session.headers.update(
            {
                "Accept": "*/*",
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Content-Type": "text/plain;charset=UTF-8",
                "Origin": "https://www.deezer.com",
                "Referer": "https://www.deezer.com/",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

        self.api_token: str = ""
        self.country: str = ""
        self.license_token: str = ""
        self.language: str = "en"
        self.renew_timestamp: float = 0
        self.available_formats: list[str] = ["MP3_128"]
        self._arl: str = ""  # Store ARL for auto-relogin

    async def close(self) -> None:
        """Close the aiohttp session."""
        if not self.session.closed:
            await self.session.close()

    async def _gw_api_call(
        self, method: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Make a call to Deezer's gateway API.

        Args:
            method: API method name.
            payload: Request payload.

        Returns:
            API response results.

        Raises:
            ModuleAPIError: If the API returns an error.
        """
        if payload is None:
            payload = {}

        # Auto-relogin if api_token is empty but we have ARL
        if (
            not self.api_token
            and self._arl
            and method not in ("deezer.getUserData", "user.getArl")
        ):
            await self._gw_api_call("deezer.getUserData")

        # Use empty token for auth methods
        api_token = (
            self.api_token
            if method not in ("deezer.getUserData", "user.getArl")
            else ""
        )

        params = {
            "method": method,
            "input": "3",
            "api_version": "1.0",
            "api_token": api_token,
            "cid": str(randint(0, 1_000_000_000)),
        }

        async with self.session.post(
            self.GW_LIGHT_URL, params=params, json=payload
        ) as response:
            data = msgspec.json.decode(await response.read())

        if data.get("error"):
            error_type = list(data["error"].keys())[0]
            error_msg = str(list(data["error"].values())[0])
            raise ModuleAPIError(
                error_code=400,
                error_message=f"{error_type}: {error_msg}",
                api_endpoint=method,
                module_name="deezer",
            )

        # Update session data for getUserData
        if method == "deezer.getUserData":
            results = data["results"]
            self.api_token = results["checkForm"]
            self.country = results["COUNTRY"]
            self.license_token = results["USER"]["OPTIONS"]["license_token"]
            self.renew_timestamp = ceil(time())
            self.language = results["USER"]["SETTING"]["global"]["language"]

            self.available_formats = ["MP3_128"]
            format_map = {"web_hq": "MP3_320", "web_lossless": "FLAC"}
            for key, fmt in format_map.items():
                if results["USER"]["OPTIONS"].get(key):
                    self.available_formats.append(fmt)

        return data["results"]

    async def _public_api_call(self, endpoint: str) -> dict[str, Any]:
        """Make a call to Deezer's public API.

        Args:
            endpoint: API endpoint path.

        Returns:
            API response data.

        Raises:
            ModuleAPIError: If the API returns an error.
        """
        async with self.session.get(f"{self.API_URL}/{endpoint}") as response:
            data = msgspec.json.decode(await response.read())

        if "error" in data:
            error = data["error"]
            error_type = error.get("type", "Unknown")
            error_msg = error.get("message", "")
            raise ModuleAPIError(
                error_code=error.get("code", 400),
                error_message=f"{error_type}: {error_msg}",
                api_endpoint=endpoint,
                module_name="deezer",
            )

        return data

    async def login_via_arl(self, arl: str) -> dict[str, Any]:
        """Authenticate using ARL cookie.

        Args:
            arl: Deezer ARL authentication cookie.

        Returns:
            User data dictionary.

        Raises:
            ModuleAuthError: If the ARL is invalid.
        """
        # Store ARL for auto-relogin
        self._arl = arl

        # Set cookie with proper domain for Deezer
        self.session.cookie_jar.update_cookies(
            {"arl": arl}, URL("https://www.deezer.com")
        )

        user_data = await self._gw_api_call("deezer.getUserData")

        if not user_data["USER"]["USER_ID"]:
            self.session.cookie_jar.clear()
            self._arl = ""
            raise ModuleAuthError(module_name="deezer")

        return user_data

    async def login_via_email(
        self, email: str, password: str
    ) -> tuple[str, dict[str, Any]]:
        """Authenticate using email and password.

        Args:
            email: User email address.
            password: User password.

        Returns:
            Tuple of (ARL token, user data dictionary).

        Raises:
            ModuleAuthError: If authentication fails.
        """
        # Get anonymous session cookie
        async with self.session.get("https://www.deezer.com"):
            pass

        password_hash = MD5.new(password.encode()).hexdigest()
        hash_input = self.client_id + email + password_hash + self.client_secret
        auth_hash = MD5.new(hash_input.encode()).hexdigest()

        params = {
            "app_id": self.client_id,
            "login": email,
            "password": password_hash,
            "hash": auth_hash,
        }

        async with self.session.get(
            "https://connect.deezer.com/oauth/user_auth.php", params=params
        ) as response:
            data = msgspec.json.decode(await response.read())

        if "error" in data:
            raise ModuleAuthError(module_name="deezer")

        arl_result = await self._gw_api_call("user.getArl")
        # user.getArl returns the ARL string directly
        arl_token: str = str(arl_result)
        user_data = await self.login_via_arl(arl_token)

        return arl_token, user_data

    async def get_track(self, track_id: str) -> dict[str, Any]:
        """Get track metadata from pageTrack endpoint.

        Args:
            track_id: Track identifier.

        Returns:
            Track data dictionary.
        """
        return await self._gw_api_call("deezer.pageTrack", {"sng_id": track_id})

    async def get_track_data(self, track_id: str) -> dict[str, Any]:
        """Get raw track data from song.getData endpoint.

        Args:
            track_id: Track identifier.

        Returns:
            Track data dictionary.
        """
        return await self._gw_api_call("song.getData", {"sng_id": track_id})

    async def get_track_lyrics(self, track_id: str) -> dict[str, Any]:
        """Get track lyrics.

        Args:
            track_id: Track identifier.

        Returns:
            Lyrics data dictionary.
        """
        return await self._gw_api_call("song.getLyrics", {"sng_id": track_id})

    async def get_track_contributors(self, track_id: str) -> dict[str, Any]:
        """Get track contributors/credits.

        Args:
            track_id: Track identifier.

        Returns:
            Contributors dictionary.
        """
        result = await self._gw_api_call(
            "song.getData",
            {"sng_id": track_id, "array_default": ["SNG_CONTRIBUTORS"]},
        )
        return result.get("SNG_CONTRIBUTORS", {})

    async def get_track_cover(self, track_id: str) -> str:
        """Get track album cover MD5 hash.

        Args:
            track_id: Track identifier.

        Returns:
            Album cover MD5 hash string.
        """
        result = await self._gw_api_call(
            "song.getData",
            {"sng_id": track_id, "array_default": ["ALB_PICTURE"]},
        )
        return result.get("ALB_PICTURE", "")

    async def get_album(self, album_id: str) -> dict[str, Any]:
        """Get album metadata and track list.

        Args:
            album_id: Album identifier.

        Returns:
            Album data dictionary.

        Raises:
            ModuleAPIError: If album not found.
        """
        try:
            return await self._gw_api_call(
                "deezer.pageAlbum", {"alb_id": album_id, "lang": self.language}
            )
        except ModuleAPIError as e:
            # Try fallback album if available
            if "FALLBACK" in str(e):
                fallback_data = await self._gw_api_call(
                    "album.getData", {"alb_id": album_id}
                )
                if "FALLBACK" in fallback_data:
                    return await self._gw_api_call(
                        "deezer.pageAlbum",
                        {
                            "alb_id": fallback_data["FALLBACK"]["ALB_ID"],
                            "lang": self.language,
                        },
                    )
            raise

    async def get_album_genres(self, album_id: str) -> list[str]:
        """Get album genres from public API.

        Args:
            album_id: Album identifier.

        Returns:
            List of genre names.
        """
        data = await self._public_api_call(f"album/{album_id}")
        genres = data.get("genres", {}).get("data", [])
        return [g["name"] for g in genres]

    async def get_playlist(
        self, playlist_id: str, limit: int = -1, start: int = 0
    ) -> dict[str, Any]:
        """Get playlist metadata and tracks.

        Args:
            playlist_id: Playlist identifier.
            limit: Maximum number of tracks (-1 for all).
            start: Starting offset.

        Returns:
            Playlist data dictionary.
        """
        return await self._gw_api_call(
            "deezer.pagePlaylist",
            {
                "nb": limit,
                "start": start,
                "playlist_id": playlist_id,
                "lang": self.language,
                "tab": 0,
                "tags": True,
                "header": True,
            },
        )

    async def get_artist_name(self, artist_id: str) -> str:
        """Get artist name.

        Args:
            artist_id: Artist identifier.

        Returns:
            Artist name string.
        """
        result = await self._gw_api_call(
            "artist.getData", {"art_id": artist_id, "array_default": ["ART_NAME"]}
        )
        return result.get("ART_NAME", "")

    async def get_artist_album_ids(
        self, artist_id: str, start: int = 0, limit: int = -1, credited: bool = False
    ) -> list[str]:
        """Get artist's album IDs.

        Args:
            artist_id: Artist identifier.
            start: Starting offset.
            limit: Maximum number of albums (-1 for all).
            credited: Include albums where artist is credited.

        Returns:
            List of album IDs.
        """
        payload = {
            "art_id": artist_id,
            "start": start,
            "nb": limit,
            "filter_role_id": [0, 5] if credited else [0],
            "nb_songs": 0,
            "discography_mode": "all" if credited else None,
            "array_default": ["ALB_ID"],
        }
        result = await self._gw_api_call("album.getDiscography", payload)
        return [a["ALB_ID"] for a in result.get("data", [])]

    async def search(
        self, query: str, search_type: str, start: int = 0, limit: int = 20
    ) -> dict[str, Any]:
        """Search for content.

        Args:
            query: Search query string.
            search_type: Type of content (TRACK, ALBUM, ARTIST, PLAYLIST).
            start: Starting offset.
            limit: Maximum number of results.

        Returns:
            Search results dictionary.
        """
        return await self._gw_api_call(
            "search.music",
            {
                "query": query,
                "start": start,
                "nb": limit,
                "filter": "ALL",
                "output": search_type.upper(),
            },
        )

    async def get_track_by_isrc(self, isrc: str) -> dict[str, Any]:
        """Get track data by ISRC.

        Args:
            isrc: International Standard Recording Code.

        Returns:
            Track data dictionary.

        Raises:
            ModuleAPIError: If track not found.
        """
        data = await self._public_api_call(f"track/isrc:{isrc}")
        return {
            "SNG_ID": str(data["id"]),
            "SNG_TITLE": data["title_short"],
            "VERSION": data.get("title_version", ""),
            "ARTISTS": [{"ART_NAME": a["name"]} for a in data.get("contributors", [])],
            "EXPLICIT_LYRICS": str(int(data.get("explicit_lyrics", False))),
            "ALB_TITLE": data.get("album", {}).get("title", ""),
        }

    async def get_track_url(
        self,
        track_id: str,
        track_token: str,
        track_token_expiry: float,
        audio_format: str,
    ) -> str:
        """Get track streaming URL.

        Args:
            track_id: Track identifier.
            track_token: Track authentication token.
            track_token_expiry: Token expiry timestamp.
            audio_format: Audio format (MP3_128, MP3_320, FLAC).

        Returns:
            Streaming URL string.
        """
        # Renew license token if expired (1 hour)
        if time() - self.renew_timestamp >= 3600:
            await self._gw_api_call("deezer.getUserData")

        # Renew track token if expired
        if time() >= track_token_expiry:
            result = await self._gw_api_call(
                "song.getData",
                {"sng_id": track_id, "array_default": ["TRACK_TOKEN"]},
            )
            track_token = result["TRACK_TOKEN"]

        payload = {
            "license_token": self.license_token,
            "media": [
                {
                    "type": "FULL",
                    "formats": [{"cipher": "BF_CBC_STRIPE", "format": audio_format}],
                }
            ],
            "track_tokens": [track_token],
        }

        async with self.session.post(self.MEDIA_URL, json=payload) as response:
            data = msgspec.json.decode(await response.read())

        return data["data"][0]["media"][0]["sources"][0]["url"]

    def get_blowfish_key(self, track_id: str) -> bytes:
        """Generate Blowfish decryption key for a track.

        Args:
            track_id: Track identifier.

        Returns:
            16-byte Blowfish key.
        """
        # MD5 hash of track ID as hex string bytes
        md5_hash = md5(str(track_id).encode()).hexdigest().encode("ascii")

        # XOR first 16 bytes with second 16 bytes and secret
        key = bytes(
            md5_hash[i] ^ md5_hash[i + 16] ^ self.bf_secret[i] for i in range(16)
        )
        return key

    def _create_blowfish_decryptor(
        self, bf_key: bytes, chunk_size: int = 1048576
    ) -> Callable[[bytes, int], bytes]:
        """Create a chunk processor for Blowfish CBC decryption.

        Deezer encrypts every third 2048-byte block with Blowfish CBC.

        Args:
            bf_key: 16-byte Blowfish decryption key.
            chunk_size: Chunk size in bytes (must match download_file chunk_size
                and be a multiple of 2048). Defaults to 1 MiB (1048576 bytes).

        Returns:
            A chunk processor function for use with download_file.

        Raises:
            ValueError: If chunk_size is not a multiple of 2048.
        """
        block_size = 2048
        if chunk_size % block_size != 0:
            raise ValueError(
                f"chunk_size must be a multiple of {block_size}, got {chunk_size}"
            )

        iv = b"\x00\x01\x02\x03\x04\x05\x06\x07"
        blocks_per_chunk = chunk_size // block_size

        def process_chunk(chunk: bytes, chunk_index: int) -> bytes:
            result = bytearray()
            # Starting block index for this chunk
            base_block_index = chunk_index * blocks_per_chunk

            for i in range(0, len(chunk), block_size):
                block = chunk[i : i + block_size]
                block_index = base_block_index + (i // block_size)

                # Decrypt every third block (index 0, 3, 6, ...)
                if block_index % 3 == 0 and len(block) == block_size:
                    cipher = Blowfish.new(bf_key, Blowfish.MODE_CBC, iv)
                    block = cipher.decrypt(block)

                result.extend(block)

            return bytes(result)

        return process_chunk

    async def download_and_decrypt_track(
        self,
        track_id: str,
        url: str,
        output_path: str,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Download and decrypt a Deezer track with streaming decryption.

        Decrypts the track during download using Blowfish CBC
        (every third 2048-byte block), avoiding temporary files.

        Args:
            track_id: Track identifier for key generation.
            url: Encrypted stream URL.
            output_path: Path to save decrypted file.
            session: Optional aiohttp session to reuse.
        """
        chunk_size = 1048576
        bf_key = self.get_blowfish_key(track_id)
        chunk_processor = self._create_blowfish_decryptor(bf_key, chunk_size)

        await download_file(
            url,
            output_path,
            session=session,
            chunk_processor=chunk_processor,
            chunk_size=chunk_size,
        )
