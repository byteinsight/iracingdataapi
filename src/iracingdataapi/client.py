import base64
import csv
from datetime import datetime, timedelta
import hashlib
from io import StringIO
import logging
import random
import requests
import time
from typing import Dict, Optional, Union, Any
from urllib.parse import urlparse

logger = logging.getLogger("system_logger")


class irDataClient:

    def __init__(self, username=None, password=None, silent=False):
        self.authenticated = False
        self.session = requests.Session()
        self.base_url = "https://members-ng.iracing.com"
        self.silent = silent

        self.username = username
        self.encoded_password = self._encode_password(username, password)

    @staticmethod
    def _encode_password(username: str, password: str) -> str:
        """
        Encodes a password by hashing it with the username and encoding the result in Base64.

        This function concatenates the password with the lowercase username, hashes the result
        using SHA-256, and returns the hash encoded in Base64 format.

        Args:
            username (str): The username associated with the password.
            password (str): The plaintext password to be encoded.

        Returns:
            str: The Base64-encoded SHA-256 hash of the combined password and username.
        """
        initial_hash = hashlib.sha256(
            (password + username.lower()).encode("utf-8")
        ).digest()

        return base64.b64encode(initial_hash).decode("utf-8")

    def _build_url(self, endpoint: str) -> str:
        """
        Constructs a full URL by appending an endpoint to the base URL.

        Args:
            endpoint (str): The API endpoint to append to the base URL.

        Returns:
            str: The full URL formed by combining the base URL and the endpoint.
        """
        return self.base_url + endpoint

    @staticmethod
    def get_main_url(any_url: str) -> str:
        """
        Extracts and returns the main part of a URL (scheme, domain, and path).

        This function parses the input URL and reconstructs it using the scheme,
        network location (domain), and path, omitting query parameters and fragments.

        Args:
            any_url (str): The full URL to process.

        Returns:
            str: The reconstructed URL containing only the scheme, domain, and path.
        """
        parsed = urlparse(any_url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    @staticmethod
    def _calculate_backoff(attempt: int, ratelimit_reset: Optional[str]) -> float:
        """
        Calculates the backoff time before retrying a request, based on rate limit reset or exponential backoff.

        If a rate limit reset time is provided, the function calculates the time until that reset
        (plus a 500ms buffer) and returns it. Otherwise, it uses exponential backoff with jitter
        as a fallback.

        Args:
            attempt (int): The current retry attempt count (used for exponential backoff).
            ratelimit_reset (Optional[str]): A UNIX timestamp (in seconds) indicating when the
                rate limit resets. If None, exponential backoff is used.

        Returns:
            float: The number of seconds to wait before retrying, with a minimum of 1 second and
            a maximum of 60 seconds.
        """
        if ratelimit_reset:
            reset_time = datetime.fromtimestamp(int(ratelimit_reset))
            delta = reset_time - datetime.now() + timedelta(milliseconds=500)
            return max(delta.total_seconds(), 1.0)
        # fallback exponential backoff with jitter
        return min(2 ** attempt + random.uniform(0, 1), 60)

    def _login(self, retries: int = 3):
        """
        Attempts to authenticate the user with the iRacing API, using retries and backoff on failure.

        Sends a POST request with the user's credentials to the authentication endpoint. If the server
        responds with a rate limit (HTTP 429), it waits for the appropriate time before retrying,
        using rate-limit headers when available or exponential backoff with jitter as a fallback.

        The method will retry failed logins up to the specified number of attempts.

        Args:
            retries (int): The number of retry attempts allowed on failure (default is 3).

        Raises:
            RuntimeError: If authentication fails due to an unexpected response, missing auth code,
                or after all retries are exhausted.
        """

        # Prepare headers and login payload
        headers = {"Content-Type": "application/json"}
        data = {"email": self.username, "password": self.encoded_password}

        for attempt in range(retries):
            try:
                # Send POST request to authentication endpoint
                r = self.session.post(
                    "https://members-ng.iracing.com/auth",
                    headers=headers,
                    json=data,
                    timeout=5.0,
                )

                # Extract rate-limiting headers for backoff calculation
                ratelimit_reset = r.headers.get("x-ratelimit-reset")
                ratelimit_remaining = r.headers.get("x-ratelimit-remaining")
                ratelimit_limit = r.headers.get("x-ratelimit-limit")

                # Log response and rate limit info
                logger.debug(f"_login: Status: {r.status_code}; reset:{ratelimit_reset}; limit:{ratelimit_limit}; remain:{ratelimit_remaining}")

                if r.status_code == 429:
                    # Rate-limited: calculate wait time and retry after sleeping
                    wait_time = self._calculate_backoff(attempt, ratelimit_reset)
                    logger.warning(f"Login rate-limited, waiting {wait_time:.2f}s on attempt {attempt + 1}")
                    time.sleep(wait_time)
                    continue

                elif r.status_code == 200:
                    # Successful login: check for auth code
                    response_data = r.json()
                    if response_data.get("authcode"):
                        self.authenticated = True
                        time.sleep(0.5)  # Brief delay to let session stabilize
                        return
                    else:
                        # Login response missing auth code
                        raise RuntimeError(f"Auth failure: {response_data}")
                else:
                    # Unexpected HTTP status code
                    raise RuntimeError(f"Login HTTP {r.status_code}")

            except (requests.Timeout, requests.ConnectionError) as e:
                # Handle network-related exceptions with backoff
                wait_time = self._calculate_backoff(attempt, None)
                logger.warning(f"Login attempt {attempt + 1} failed: {e}, retrying in {wait_time:.2f}s")
                time.sleep(wait_time)

        # All retries exhausted without success
        raise RuntimeError("Login failed after retries")

    def _robust_get(self, url: str, payload: Optional[dict] = None, retries: int = 3, require_auth: bool = True, context: str = "") -> requests.Response:
        """
        Performs a robust HTTP GET request with retry logic, backoff, and optional authentication.

        This method handles transient errors such as timeouts, connection issues, rate limiting (429),
        and authorization issues (401/403) with retries and exponential backoff. It supports optional
        re-authentication if a 401 Unauthorized response is encountered and handles rate-limiting
        headers to determine appropriate wait times.

        Args:
            url (str): The URL to send the GET request to.
            payload (Optional[dict]): Optional query parameters to include in the request.
            retries (int): Maximum number of retry attempts for the request. Default is 3.
            require_auth (bool): Whether authentication is required before making the request.
            context (str): Optional context string for logging purposes.

        Returns:
            requests.Response: The HTTP response object for a successful request (status code 200).

        Raises:
            RuntimeError: If the request fails after retries, or on repeated authentication or forbidden errors.
        """
        # Authenticate if required and not already authenticated
        if require_auth and not self.authenticated:
            self._login()

        attempted_relogin = False  # Flag to prevent multiple re-login attempts

        for attempt in range(retries):
            try:
                # Send GET request with optional query parameters
                r = self.session.get(url, params=payload)

                # Extract rate-limiting headers for logging and backoff handling
                ratelimit_reset = r.headers.get("x-ratelimit-reset")
                ratelimit_remaining = r.headers.get("x-ratelimit-remaining")
                ratelimit_limit = r.headers.get("x-ratelimit-limit")

                # Log response status and rate limit metadata
                logger.debug(
                    f"{context or '_robust_get'}: Status: {r.status_code}; "
                    f"RL_Reset:{ratelimit_reset}; RL_Limit:{ratelimit_limit}; "
                    f"RL_Remaining:{ratelimit_remaining} for {url}"
                )

                if r.status_code == 401:
                    # Handle unauthorized access (e.g. expired token)
                    if not attempted_relogin:
                        attempted_relogin = True
                        self.authenticated = False
                        logger.warning(f"{context}: 401 Unauthorized — re-authenticating")
                        self._login()
                        continue
                    else:
                        r.close()
                        raise RuntimeError(f"{context}: 401 repeated after re-login for {url}")

                if r.status_code == 403:
                    # Forbidden access — do not retry
                    r.close()
                    logger.error(f"{context}: 403 Forbidden for {url}")
                    raise RuntimeError(f"{context}: 403 Forbidden for {url}")

                if r.status_code == 429:
                    # Rate-limited — wait and retry with backoff
                    wait_time = self._calculate_backoff(attempt, ratelimit_reset)
                    logger.warning(f"{context}: 429 Rate Limited, retrying in {wait_time:.2f}s (attempt {attempt + 1})")
                    time.sleep(wait_time)
                    r.close()
                    continue

                if r.status_code != 200:
                    # Any other non-200 response — raise error
                    r.close()
                    raise RuntimeError(f"{context}: Unhandled Non-200 response {r.status_code} for {url}")

                # Success — return the response
                return r

            except (requests.Timeout, requests.ConnectionError) as e:
                # Network-related errors — apply exponential backoff
                wait_time = self._calculate_backoff(attempt, None)
                logger.warning(f"{context}: Connection/Timeout error: {e}. Retrying in {wait_time:.1f}s (attempt {attempt + 1}/{retries})")
                time.sleep(wait_time)

        # All retry attempts exhausted — raise error
        raise RuntimeError(f"{context}: Exceeded retry attempts for {url}")

    def _get_resource_or_link(self, url: str, payload: dict = None, retries: int = 3) -> tuple[Any, bool] | tuple[list, bool]:
        """
        Fetches a resource from a given URL and determines if it contains a direct link or a full data object.

        This method performs a robust GET request and parses the JSON response. If the response is not a list
        and contains a "link" key, it assumes the response is a redirect or reference and returns the link
        along with a flag indicating it's a link. Otherwise, it returns the full data and a flag indicating
        it's not a link.

        Args:
            url (str): The URL to fetch the resource from.
            payload (dict, optional): Optional query parameters for the request.
            retries (int): Number of retry attempts for the GET request. Defaults to 3.

        Returns:
            list[Union[Dict, str], bool]: A two-element list where the first item is either a dictionary
            (full resource data) or a string (link), and the second item is a boolean indicating whether
            the first item is a link (`True`) or a full resource (`False`).
        """
        r = self._robust_get(url, payload=payload, retries=retries, context="_get_resource_or_link")
        data = r.json()
        if not isinstance(data, list) and "link" in data:
            return data.get("link"), True
        return data, False

    def _get_resource(self, endpoint: str, payload: Optional[dict] = None, retries: int = 3) -> Optional[Union[list, dict]]:
        """
        Retrieves a resource from a specified API endpoint, handling linked responses and content types.

        This method builds the full request URL, fetches the resource (or link to a resource), and
        determines how to handle the response based on the content type. It supports retry logic
        and parses both JSON and CSV/text responses.

        Args:
            endpoint (str): The API endpoint to query.
            payload (Optional[dict]): Optional query parameters to include in the request.
            retries (int): The number of retry attempts allowed. Defaults to 3.

        Returns:
            Optional[Union[list, dict]]: Parsed response data if successful (either JSON or CSV),
            or None if the content type is unsupported or the request fails.
        """
        # Construct the full request URL from the endpoint
        request_url = self._build_url(endpoint)

        # Get the resource or a link to the actual resource
        resource_obj, is_link = self._get_resource_or_link(request_url, payload=payload, retries=retries)

        # If the response is not a link, return the resource directly
        if not is_link:
            return resource_obj

        # If it's a link, fetch the linked resource
        r = self._robust_get(resource_obj, retries=retries, context="_get_resource (linked resource)")

        # Determine how to handle the response based on content type
        content_type = r.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return r.json()
        elif "text/csv" in content_type or "text/plain" in content_type:
            return self._parse_csv_response(r.text)
        else:
            # Unsupported content type encountered
            logger.error(f"_get_resource: Unsupported Content-Type {content_type} for {r.url}")
            return None

    def _get_chunks(self, chunks) -> list:
        """
        Retrieves and combines JSON data from a list of chunked file URLs.

        This method expects a dictionary containing a base download URL and a list of chunk file names.
        It fetches each chunk via HTTP GET, assumes the content is JSON, and flattens the combined
        results into a single list.

        Args:
            chunks: A dictionary with keys 'base_download_url' (str) and 'chunk_file_names' (list of str),
                    or any other type that will be treated as empty.

        Returns:
            list: A flattened list containing combined data from all retrieved chunks. If the input is not
                  a dictionary, an empty list is returned.
        """
        if not isinstance(chunks, dict):
            # If chunks is not a dictionary, return empty list for compatibility
            return []

        # Extract the base URL for downloading chunks
        base_url = chunks.get("base_download_url")

        # Build full URLs for each chunk file
        urls = [base_url + x for x in chunks.get("chunk_file_names")]

        # Fetch and parse each chunk file as JSON
        list_of_chunks = [self.session.get(url).json() for url in urls]

        # Flatten the list of lists into a single list of items
        output = [item for sublist in list_of_chunks for item in sublist]

        return output

    @staticmethod
    def _add_assets(objects: list, assets: dict, id_key: str) -> list:
        """
        Enriches a list of objects with additional asset data based on a matching ID key.

        For each object in the input list, this method looks up its ID in the `assets` dictionary
        and merges the corresponding asset fields into the object.

        Args:
            objects (list): A list of dictionaries to enrich with asset data.
            assets (dict): A dictionary where keys are stringified IDs and values are dictionaries
                           containing additional data to merge into the objects.
            id_key (str): The key in each object that holds the ID used to look up its asset data.

        Returns:
            list: The list of enriched objects with asset data merged in.
        """
        for obj in objects:
            # Look up the asset using the object's ID (converted to string)
            a = assets[str(obj[id_key])]

            # Merge asset data into the object
            for key in a.keys():
                obj[key] = a[key]

        return objects

    @staticmethod
    def _parse_csv_response(text: str) -> list[Dict[str, str]]:
        """
        Parses a CSV string into a list of dictionaries, using lowercase headers as keys.

        This method reads CSV-formatted text, converts each row into a dictionary where the
        keys are the lowercase column headers, and returns a list of these dictionaries.
        It skips rows that do not match the header length and prints a warning for mismatches.

        Args:
            text (str): The CSV content as a string.

        Returns:
            list: A list of dictionaries, each representing a row from the CSV.
        """
        csv_data = []

        # Initialize CSV reader with comma delimiter
        reader = csv.reader(StringIO(text), delimiter=",")

        # Read and normalize headers (convert to lowercase)
        headers = [header.lower() for header in next(reader)]

        # Process each row, mapping values to headers
        for row in reader:
            if len(row) == len(headers):
                csv_data.append(dict(zip(headers, row)))
            else:
                # Handle mismatched row lengths
                print("Warning: Row length does not match headers length")

        return csv_data

    @property
    def cars(self) -> list[Dict]:
        """
        Retrieves the list of cars with associated asset data merged in.

        This property fetches raw car data and their corresponding asset details,
        then enriches each car object with its related assets by matching on 'car_id'.

        Returns:
            list[Dict]: A list of car dictionaries with asset data included.
        """
        # Fetch raw car data
        cars = self.get_cars()

        # Fetch asset data related to each car
        car_assets = self.get_cars_assets()

        # Enrich car data with assets based on 'car_id'
        return self._add_assets(cars, car_assets, "car_id")

    @property
    def tracks(self) -> list[Dict]:
        """
        Retrieves the list of tracks with associated asset data merged in.

        This property fetches raw track data and their corresponding asset details,
        then enriches each track object by merging the assets using 'track_id' as the key.

        Returns:
            list[Dict]: A list of track dictionaries with asset data included.
        """
        # Fetch raw track data
        tracks = self.get_tracks()

        # Fetch asset data for tracks
        track_assets = self.get_tracks_assets()

        # Merge asset data into track objects using 'track_id'
        return self._add_assets(tracks, track_assets, "track_id")

    @property
    def series(self) -> list[Dict]:
        """
        Retrieves the list of series with associated asset data merged in.

        This property fetches raw series data and their corresponding asset details,
        then enriches each series object by merging the assets using 'series_id' as the key.

        Returns:
            list[Dict]: A list of series dictionaries with asset data included.
        """
        # Fetch raw series data
        series = self.get_series()

        # Fetch asset data for each series
        series_assets = self.get_series_assets()

        # Merge asset data into series objects using 'series_id'
        return self._add_assets(series, series_assets, "series_id")

    def constants_categories(self) -> list[Dict]:
        """Fetches a list containing the racing categories.

        Retrieves a list containing the racing categories and
        the id associated to each category.

        Returns:
            list: A list of dicts representing each category.

        """
        return self._get_resource("/data/constants/categories")

    def constants_divisions(self) -> list[Dict]:
        """Fetches a list containing the racing divisions.

        Retrieves a list containing the racing divisions and
        the id associated to each division.

        Returns:
            list: A list of dicts representing each division.

        """
        return self._get_resource("/data/constants/divisions")

    def constants_event_types(self) -> list[Dict]:
        """Fetches a list containing the event types.

        Retrieves a list containing the types of events (i.e. Practice,
        Qualify, Time Trial or Race)

        Returns:
            list: A list of dicts representing each event type.

        """
        return self._get_resource("/data/constants/event_types")

    def driver_list(self, category_id: int = None) -> list[Dict]:
        """Fetches driver list by racing category

        Retrieves a list containing the driver data by category
        (category_id (int): 1 - Oval; 2 - Road; 3 - Dirt oval; 4 - Dirt road; 5 - Sports Car; 6 - Formula Car)

        Returns:
            list: A list of dicts representing driver data.

        """
        category_endpoints = {
            1: "/data/driver_stats_by_category/oval",
            2: "/data/driver_stats_by_category/road",
            3: "/data/driver_stats_by_category/dirt_oval",
            4: "/data/driver_stats_by_category/dirt_road",
            5: "/data/driver_stats_by_category/sports_car",
            6: "/data/driver_stats_by_category/formula_car",
        }

        if category_id not in category_endpoints:
            raise ValueError(
                f"Invalid category_id '{category_id}'. Available categories are: {list(category_endpoints.keys())}"
            )

        endpoint = category_endpoints[category_id]
        return self._get_resource(endpoint)

    def get_cars(self) -> list[Dict]:
        """Fetches a list containing all the cars in the service.

        Retrieves a list containing each car in the service, with
        detailed information related to the car within iRacing.

        Returns:
            list: A list of dicts representing each car information.
        """
        return self._get_resource("/data/car/get")

    def get_cars_assets(self) -> Dict:
        """Fetches a list containing all the car assets in the service.

        Retrieves a list containing each car in the service, with
        detailed assets related to the car (i.e. photos, tech specs, etc.)

        Returns:
            dict: a dict with keys relating to each car id, each containing a further dict of car assets.
        """
        return self._get_resource("/data/car/assets")

    def get_carclasses(self) -> list[Dict]:
        """Fetches a list containing all the car classes in the service.

        Retrieves a list containing each car class in the service, including
        all the cars in class and basic information of the car class.

        Returns:
            list: A list of dicts representing assets from each car.

        """
        return self._get_resource("/data/carclass/get")

    def get_tracks(self) -> list[Dict]:
        """Fetches a list containing all the tracks in the service.

        Retrieves a list containing each track in the service, with
        detailed information related to the track within iRacing.

        Returns:
            list: A list of dicts representing each track information.
        """
        return self._get_resource("/data/track/get")

    def get_tracks_assets(self) -> Dict:
        """Fetches a dict containing all the track assets in the service.

        Retrieves a dict containing each track in the service, with
        detailed assets related to the track (i.e. photos, track map, etc.)

        Returns:
            dict: a dict with keys relating to each track id, each containing a further dict of track assets.
        """
        return self._get_resource("/data/track/assets")

    def hosted_combined_sessions(self, package_id: int = None) -> Dict:
        """Fetches a dict containing the combined hosted sessions

        Retrieves a dict containing all the hosted sessions (available through ``sessions`` key)
        including:

        - Hosted sessions that can be joined as driver
        - Hosted sessions that can be joined as spectator
        - Non-league pending sessions for the user

        Args:
            package_id (int): If set, return only sessions using this car or track package ID.

        Returns:
            dict: A dict containing all the combined hosted sessions.
        """
        payload = {}
        if package_id:
            payload["package_id"] = package_id
        return self._get_resource("/data/hosted/combined_sessions", payload=payload)

    def hosted_sessions(self) -> Dict:
        """Fetches a dict containing the hosted sessions

        Retrieves a dict containing the hosted sessions (available through ``sessions`` key)
        but only **hosted sessions that can be joined as driver.**

        Returns:
            dict: A dict containing all the combined hosted sessions.
        """
        return self._get_resource("/data/hosted/sessions")

    def league_get(self, league_id: int = None, include_licenses: bool = False) -> Dict:
        """Fetches a dict containing information from the league requested.

        Retrieves a dict containing all the information of the league requested.

        Args:
            league_id (int): the ID of the league requested
            include_licenses (bool): whether if you want to include the licenses or not.
                Default ``False``.

        Returns:
            dict: A dict containing all the information of the league requested.
        """
        if not league_id:
            raise RuntimeError("Please supply a league_id")
        payload = {"league_id": league_id, "include_licenses": include_licenses}
        return self._get_resource("/data/league/get", payload=payload)

    def league_cust_league_sessions(
        self, mine: bool = False, package_id: int = None
    ) -> Dict:
        """Fetches a dict containing information from the league requested.

        Retrieves a dict containing all the information of the league requested.

        Args:
            mine (bool): If ``True``, return only sessions created by this user.
            package_id (int): If set, return only sessions using this car or track package ID.

        Returns:
            dict: A dict containing all the information of the league requested.
        """
        payload = {"mine": mine}
        if package_id:
            payload["package_id"] = package_id

        return self._get_resource("/data/league/cust_league_sessions", payload=payload)

    def league_directory(
        self,
        search: str = "",
        tag: str = "",
        restrict_to_member: bool = False,
        restrict_to_recruiting: bool = False,
        restrict_to_friends: bool = False,
        restrict_to_watched: bool = False,
        minimum_roster_count: int = 0,
        maximum_roster_count: int = 999,
        lowerbound: int = 1,
        upperbound: Optional[int] = None,
        sort: Optional[str] = None,
        order: str = "asc",
    ) -> Dict:
        """Fetches the iRacing leagues that matches the parameters requested

        Retrieves the iRacing leagues that matches the parameters requested by the user.

        Args:
            search (str): Will search against league name, description, owner, and league ID.
            tag (str): One or more tags, comma-separated. (i.e. ``"tag1,tag2"``)
            restrict_to_member (bool): If ``True`` include only leagues for which customer is a member.
            restrict_to_recruiting (bool): If ``True`` include only leagues which are recruiting.
            restrict_to_friends: If ``True`` include only leagues owned by a friend.
            restrict_to_watched: If ``True`` include only leagues owned by a watched member.
            minimum_roster_count (int): If set include leagues with at least this number of members.
            maximum_roster_count (int): If set include leagues with no more than this number of members.
                Defaults to 999.
            lowerbound (int): First row of results to return.  Defaults to 1.
            upperbound (int): Last row of results to return. Defaults to lowerbound + 39.
            sort (str): One of relevance, leaguename, displayname, rostercount.
                Displayname is owners' name. Defaults to relevance.
            order (str): One of ``asc`` or ``desc``. Defaults to asc.

        Returns:
            dict: a dict with the request info including the leagues from the search requested.

        """
        params = locals()
        payload = {}
        for x in params.keys():
            if x != "self":
                payload[x] = params[x]

        return self._get_resource("/data/league/directory", payload=payload)

    def league_get_points_systems(self, league_id: int, season_id: int = None) -> Dict:
        """Fetches a dict containing the point system from the league requested.

        Retrieves a dict containing all the information of the league requested.

        Args:
            league_id (int): the id of the league to retrieve the points systems.
            season_id (int): if included and the season is using custom points (points_system_id:2)
             then the custom points option is included in the returned list. Otherwise, the custom points option
             is not returned.

        Returns:
            dict: A dict containing all the requested points systems of the league requested.
        """
        payload = {"league_id": league_id}
        if season_id:
            payload["season_id"] = season_id

        return self._get_resource("/data/league/get_points_systems", payload=payload)

    def league_membership(self, include_league: bool = False) -> list[Dict]:
        """Fetches a list containing all the leagues the user is currently a member.

        Retrieves a list containing all the leagues the user is member.

        Args:
            include_league (bool): if ``True``, also receives the league detailed information in ``league``.

        Returns:
            list: A list containing all the leagues the user is member.
        """
        payload = {"include_league": include_league}
        return self._get_resource("/data/league/membership", payload=payload)

    def league_roster(self, league_id: int, include_licenses: bool = False) -> Dict:
        """Fetches a dict containing information about the league roster.
        Args:
            league_id (int): the league to retrieve the roster
            include_licenses (bool): if ``True``, also receives license information.
             For faster responses, only request when necessary.

        Returns:
            dict: A dict containing the league roster.
        """
        payload = {"league_id": league_id}
        if include_licenses:
            payload["include_licenses"] = include_licenses

        resource = self._get_resource("/data/league/roster", payload=payload)

        return self._get_resource_or_link(resource["data_url"])[0]

    def league_seasons(self, league_id: int, retired: bool = False) -> list[Dict]:
        """Fetches a list containing all the seasons from a league.

        Retrieves a list containing all the seasons from a league.

        Args:
            league_id (int): the league to retrieve the seasons
            retired (bool): Default ``False``. If ``True``, includes seasons which are no longer active.

        Returns:
            list: A list containing all the seasons from a league.
        """
        payload = {"league_id": league_id, "retired": retired}
        return self._get_resource("/data/league/seasons", payload=payload)

    def league_season_standings(
        self,
        league_id: int,
        season_id: int,
        car_class_id: Optional[int] = None,
        car_id: Optional[int] = None,
    ) -> Dict:
        """Fetches a dict containing all the seasons from a league.

        Retrieves a dict containing all the seasons from a league.

        Args:
            league_id (int): the league to retrieve the standings
            season_id (int): the season to retrieve the standings
            car_class_id (int): the car class id. see :meth:`client.irDataClient.get_carclasses`
             for more info.
            car_id (int): if provided, the car id. see :meth:`client.irDataClient.get_cars`
             for more info.

        Returns:
            dict: A dict containing the season standings from a league season.
        """
        payload = {"league_id": league_id, "season_id": season_id}
        if car_class_id:
            payload["car_class_id"] = car_class_id
        if car_id:
            payload["car_id"] = car_id

        return self._get_resource("/data/league/season_standings", payload=payload)

    def league_season_sessions(
        self, league_id: int, season_id: int, results_only: bool = False
    ) -> Dict:
        """Fetches a dict containing all the sessions from a league session.

        Retrieves a dict containing all the sessions from a league session.

        Args:
            league_id (int): the league session to retrieve the sessions
            season_id (int): the session to retrieve the sessions
            results_only (bool): if ``True`` include only sessions for which results are available.

        Returns:
            dict: A dict containing the session from a league season.
        """
        payload = {
            "league_id": league_id,
            "season_id": season_id,
            "results_only": results_only,
        }
        return self._get_resource("/data/league/season_sessions", payload=payload)

    def lookup_club_history(self, season_year: int, season_quarter: int) -> list[Dict]:
        """The club history for a year and season.

        Note: returns an earlier history if requested quarter does not have a club history

        Args:
            season_year (int): the season year
            season_quarter (int): the season quarter (1, 2, 3, 4)

        Returns:
            list: a list containing all the history from all clubs in the requested year and season.

        """
        payload = {"season_year": season_year, "season_quarter": season_quarter}
        return self._get_resource("/data/lookup/club_history", payload=payload)

    def lookup_countries(self) -> list[Dict]:
        """The list of country names and the country codes.

        Returns:
            list: a list containing all the country names and country codes.

        """
        return self._get_resource("/data/lookup/countries")

    def lookup_drivers(
        self, search_term: str = None, league_id: int = None
    ) -> list[Dict]:
        """Lookup for drivers given a search term.

        Retrieves a list of drivers given a search term. It can be narrowed
        the search filtering by league.

        Args:
            search_term (str): a cust_id or partial name for which to search.
            league_id (int): narrow the search to the roster of the given league.

        Returns:
            list: a list of drivers that matches the search terms.

        """
        payload = {"search_term": search_term}
        if league_id:
            payload["league_id"] = league_id

        return self._get_resource("/data/lookup/drivers", payload=payload)

    def lookup_get(self) -> list:
        return self._get_resource("/data/lookup/get")

    def lookup_licenses(self) -> list[Dict]:
        """All the iRacing licenses.

        Retrieves a list containing all the current licenses, from Rookie to Pro/WC.

        Returns:
            list: a list containing all the current licenses, from Rookie to Pro/WC.

        """
        return self._get_resource("/data/lookup/licenses")

    def result(self, subsession_id: int, include_licenses: bool = False) -> Dict:
        """Get the results from a specific session.

        Get the results of a subsession, if authorized to view them.
        series_logo image paths are relative to
        https://images-static.iracing.com/img/logos/series/

        Args:
            subsession_id: the unique id of the subsession from iRacing.
            include_licenses: whether if you want to include the licenses for that session.
             Default ``False``.

        Returns:
            dict: a dict containing all the result information from a subsession.

        """
        payload = {"subsession_id": subsession_id, "include_licenses": include_licenses}
        return self._get_resource("/data/results/get", payload=payload)

    def result_lap_chart_data(
        self, subsession_id: int, simsession_number: int = 0
    ) -> list[Dict]:
        """Get all the lap data from a subsession.

        Retrieves all the lap data from all cars during a sim session (Practice,
        Qualifying, Race)

        Args:
            subsession_id: the unique id of the subsession from iRacing.
            simsession_number: -2 for practice, -1 for qualy, 0 for race.
             Default ``0``.

        Returns:
            list: A list of all the laps completed by all cars.

        """
        payload = {
            "subsession_id": subsession_id,
            "simsession_number": simsession_number,
        }
        resource = self._get_resource("/data/results/lap_chart_data", payload=payload)
        return self._get_chunks(resource.get("chunk_info"))

    def result_lap_data(
        self,
        subsession_id: int,
        simsession_number: int = 0,
        cust_id: Optional[int] = None,
        team_id: Optional[int] = None,
    ) -> list[Optional[Dict]]:
        """Get the lap data from a car within a sim session.

         If there are no chunks to get, that's because no laps were done by this cust_id
         on this subsession, return an empty list for compatibility.

        Args:
            subsession_id (int): the unique id of the subsession from iRacing.
            simsession_number (int): -2 for practice, -1 for qualy, 0 for race. Default ``0``.
            cust_id (int): required if the subsession was a single-driver event. Optional for team events.
             If omitted for a team event then the laps driven by all the team's drivers will be included.
            team_id (int): required if the subsession was a team event.

        Returns:
            list: a list containing the lap data from a car within a sim session.

        """
        if not cust_id and not team_id:
            raise RuntimeError("Please supply either a cust_id or a team_id")

        payload = {
            "subsession_id": subsession_id,
            "simsession_number": simsession_number,
        }
        if cust_id:
            payload["cust_id"] = cust_id
        if team_id:
            payload["team_id"] = team_id

        resource = self._get_resource("/data/results/lap_data", payload=payload)
        if resource.get("chunk_info"):
            return self._get_chunks(resource.get("chunk_info"))

        # if there are no chunks to get, that's because no laps were done by this cust_id
        # on this subsession, return an empty list for compatibility
        return []

    def result_event_log(
        self, subsession_id: int, simsession_number: int = 0
    ) -> list[Dict]:
        """Get all the logs from a result sim session.

        Retrieves a list of all events logged during a sim event (Practice, Qualifying, Race)...

        Args:
            subsession_id (int): the unique id of the subsession from iRacing.
            simsession_number (int): -2 for practice, -1 for qualy, 0 for race. Default ``0``.

        Returns:
            list: A list of all the logged events during a sim session.

        """
        payload = {
            "subsession_id": subsession_id,
            "simsession_number": simsession_number,
        }
        resource = self._get_resource("/data/results/event_log", payload=payload)
        return self._get_chunks(resource.get("chunk_info"))

    def result_search_hosted(
        self,
        start_range_begin: Optional[str] = None,
        start_range_end: Optional[str] = None,
        finish_range_begin: Optional[str] = None,
        finish_range_end: Optional[str] = None,
        cust_id: Optional[int] = None,
        host_cust_id: Optional[int] = None,
        session_name: Optional[str] = None,
        league_id: Optional[int] = None,
        league_season_id: Optional[int] = None,
        car_id: Optional[int] = None,
        track_id: Optional[int] = None,
        category_ids: Optional[list[int]] = None,
        team_id: Optional[int] = None,
    ) -> list[Dict]:
        """Search for hosted and league sessions.

        Hosted and league sessions. Maximum time frame of 90 days.
        Results split into one or more files with chunks of results.
        For scraping results the most effective approach is to keep track of the maximum end_time
        found during a search then make the subsequent call using that date/time as the finish_range_begin
        and skip any subsessions that are duplicated.

        Results are ordered by subsessionid which is a proxy for start time

        Requires one of: start_range_begin, finish_range_begin AND one of: cust_id, team_id, host_cust_id, session_name

        Args:
            start_range_begin (str): Session start times. ISO-8601 UTC time zero offset: "2022-04-01T15:45Z"
            start_range_end (str): ISO-8601 UTC time zero offset: "2022-04-01T15:45Z".
             Exclusive. May be omitted if start_range_begin is less than 90 days in the past.
            finish_range_begin (str): Session finish times. ISO-8601 UTC time zero offset: "2022-04-01T15:45Z".
            finish_range_end (str): ISO-8601 UTC time zero offset: "2022-04-01T15:45Z".
             Exclusive. May be omitted if finish_range_begin is less than 90 days in the past.
            cust_id (int): The participant's customer ID.
            host_cust_id (int): The host's customer ID.
            session_name (str): Part or all of the session's name.
            league_id (int): Include only results for the league with this ID.
            league_season_id (int): Include only results for the league season with this ID.
            car_id (int): One of the cars used by the session.
            track_id (int): The ID of the track used by the session.
            category_ids (list[int]): Track categories to include in the search (1,2,3,4).  Defaults to all.
            team_id (int): The team ID to search for. Takes priority over cust_id if both are supplied.
        Returns:
            list: a list containing all the hosted results matching criteria.

        """
        if not (start_range_begin or finish_range_begin):
            raise RuntimeError(
                "Please supply either start_range_begin or finish_range_begin"
            )

        if not (cust_id or host_cust_id or session_name or team_id):
            raise RuntimeError(
                "Please supply one of: cust_id, host_cust_id, session_name, or team_id"
            )

        params = locals()
        payload = {}
        for x in params.keys():
            if x != "self" and params[x]:
                payload[x] = params[x]

        resource = self._get_resource("/data/results/search_hosted", payload=payload)
        return self._get_chunks(resource.get("data", dict()).get("chunk_info"))

    def result_search_series(
        self,
        season_year: Optional[int] = None,
        season_quarter: Optional[int] = None,
        start_range_begin: Optional[str] = None,
        start_range_end: Optional[str] = None,
        finish_range_begin: Optional[str] = None,
        finish_range_end: Optional[str] = None,
        cust_id: Optional[int] = None,
        series_id: Optional[int] = None,
        race_week_num: Optional[int] = None,
        official_only: bool = True,
        event_types: Optional[list[int]] = None,
        category_ids: Optional[list[int]] = None,
    ) -> list[Dict]:
        """Search for official series sessions.

        Official series.  Maximum time frame of 90 days. Results split into one or more files with chunks
        of results. For scraping results the most effective approach is to keep track
        of the maximum end_time found during a search then make the subsequent call using that
        date/time as the finish_range_begin and skip any subsessions that are duplicated.
        Results are ordered by subsessionid which is a proxy for start time but groups
        together multiple splits of a series when multiple series launch sessions at the same time.
        Requires at least one of: season_year and season_quarter, start_range_begin, finish_range_begin.

        Args:
            season_year (int): the season year
            season_quarter (int): the season quarter (1, 2, 3, 4)
            start_range_begin (str): Session start times. ISO-8601 UTC time zero offset: "2022-04-01T15:45Z"
             Exclusive. May be omitted if finish_range_begin is less than 90 days in the past.
            start_range_end (str): ISO-8601 UTC time zero offset: "2022-04-01T15:45Z".
             Exclusive. May be omitted if start_range_begin is less than 90 days in the past.
            finish_range_begin (str): Session finish times. ISO-8601 UTC time zero offset: "2022-04-01T15:45Z".
            finish_range_end (str): ISO-8601 UTC time zero offset: "2022-04-01T15:45Z".
            cust_id (int): The participant's customer ID.
            series_id (id): Include only sessions for series with this ID.
            race_week_num (id): Include only sessions with this race week number.
            official_only (bool): If true, include only sessions earning championship points. Defaults to all.
            event_types (list[int]): Types of events to include in the search. Defaults to all. event_types=2,3,4,5
            category_ids (list[int]): Track categories to include in the search (1,2,3,4).  Defaults to all.

        Returns:
            list: a list containing all the hosted results matching criteria.

        """
        if not (
            (season_year and season_quarter) or start_range_begin or finish_range_begin
        ):
            raise RuntimeError(
                "Please supply Season Year and Season Quarter or a date range"
            )

        params = locals()
        payload = {}
        for x in params.keys():
            if x != "self" and params[x]:
                payload[x] = params[x]

        resource = self._get_resource("/data/results/search_series", payload=payload)
        return self._get_chunks(resource.get("data", dict()).get("chunk_info"))

    def result_season_results(
        self,
        season_id: int,
        event_type: Optional[int] = None,
        race_week_num: Optional[int] = None,
    ) -> Dict:
        """Get results from a certain race week from a series season.

        Args:
            season_id (int): the id of the session.
            event_type (int): Restrict to one event type: 2 - Practice; 3 - Qualify; 4 - Time Trial; 5 - Race
            race_week_num (int): The first race week of a season is 0.

        Returns:
            dict: a dict containing a list of sessions within the matching criteria.

        """
        payload = {"season_id": season_id}
        if event_type:
            payload["event_type"] = event_type
        if race_week_num is not None:
            payload["race_week_num"] = race_week_num

        return self._get_resource("/data/results/season_results", payload=payload)

    def member(self, cust_id: int, include_licenses: bool = False) -> Dict:
        """Get member profile basic information from one or more members.

        Args:
            cust_id (Union[int, str]): one of several cust_id.
             for one cust_id, use an ``int``. for more cust_ids, use ``str`` separated with commas.
             i.e. ``"custid1,custid2"``
            include_licenses (bool): whether if you want to include the licenses.
             Default ``False``.

        Returns:
            dict: a dict containing the information of one or more members in ``'members'`` section.

        """
        payload = {"cust_ids": cust_id, "include_licenses": include_licenses}
        return self._get_resource("/data/member/get", payload=payload)

    def member_awards(self, cust_id: Optional[int] = None) -> list[Dict]:
        """Fetches a dict containing information on the members awards.
        Args:
            cust_id (int): the iRacing cust_id. Defaults to the authenticated member.

        Returns:
            list: A list of dicts containing all the members awards.  On failure, returns an empty list.
        """
        payload = {}
        if cust_id:
            payload["cust_id"] = cust_id

        resource = self._get_resource("/data/member/awards", payload=payload)

        return self._get_resource_or_link(resource["data_url"])[0]

    def member_chart_data(
        self, cust_id: Optional[int] = None, category_id: int = 2, chart_type: int = 1
    ) -> Dict:
        """Get the irating, ttrating or safety rating chart data of a certain category.

        Args:
            cust_id (int): the iRacing cust_id. Defaults to the authenticated member.
            category_id (int): 1 - Oval; 2 - Road; 3 - Dirt oval; 4 - Dirt road; 5 - Sports Car; 6 - Formula Car
            chart_type (int): 1 - iRating; 2 - TT Rating; 3 - License/SR

        Returns:
            dict: a dict containing the time series chart data given the matching criteria.

        """
        payload = {"category_id": category_id, "chart_type": chart_type}
        if cust_id:
            payload["cust_id"] = cust_id

        return self._get_resource("/data/member/chart_data", payload=payload)

    def member_info(self) -> Dict:
        """Account info from the authenticated member.

        Returns:
            dict: a dict containing the detailed information from the authenticated member.

        """
        return self._get_resource("/data/member/info")

    def member_profile(self, cust_id: Optional[int] = None) -> Dict:
        """Detailed profile info from a member.

        Args:
            cust_id (int): The iRacing cust_id. Defaults to the authenticated member.

        Returns:
            dict: a dict containing the detailed profile info from the member requested.

        """
        payload = {}
        if cust_id:
            payload["cust_id"] = cust_id
        return self._get_resource("/data/member/profile", payload=payload)

    def stats_member_bests(
        self, cust_id: Optional[int] = None, car_id: Optional[int] = None
    ) -> Dict:
        """Get the member best laptimes from a certain cust_id and car_id.

        Args:
            cust_id (int): The iRacing cust_id. Defaults to the authenticated member.
            car_id (int): The car id. First call should exclude car_id;
             use cars_driven list in return for subsequent calls.

        Returns:
            dict: a dict containing the member best laptimes

        """
        payload = {}
        if cust_id:
            payload["cust_id"] = cust_id
        if car_id:
            payload["car_id"] = car_id

        return self._get_resource("/data/stats/member_bests", payload=payload)

    def stats_member_career(self, cust_id: Optional[int] = None) -> Dict:
        """Get the member career stats from a certain cust_id

        Args:
            cust_id (int): The iRacing cust_id. Defaults to the authenticated member.

        Returns:
            dict: a dict containing the member career stats

        """
        payload = {}
        if cust_id:
            payload["cust_id"] = cust_id
        return self._get_resource("/data/stats/member_career", payload=payload)

    def stats_member_recap(
        self, cust_id: int = None, year: int = None, quarter: int = None
    ) -> Dict:
        """Get a recap for the member.

        Args:
            cust_id (int): The iRacing cust_id. Defaults  to the authenticated member.
            year (int): Season year; if not supplied the current calendar year (UTC) is used.
            quarter (int): Season (quarter) within the year; if not supplied the recap will be fore the entire year.

        Returns:
            dict: a dict containing a recap from the requested season/quarter/member
        """
        payload = {}
        if cust_id:
            payload["cust_id"] = cust_id
        if year:
            payload["year"] = year
        if quarter:
            payload["season"] = quarter
        return self._get_resource("/data/stats/member_recap", payload=payload)

    def stats_member_recent_races(self, cust_id: Optional[int] = None) -> Dict:
        """Get the latest member races from a certain cust_id

        Args:
            cust_id (int): The iRacing cust_id. Defaults to the authenticated member.

        Returns:
            dict: a dict containing the latest member races

        """
        payload = {}
        if cust_id:
            payload = {"cust_id": cust_id}

        return self._get_resource("/data/stats/member_recent_races", payload=payload)

    def stats_member_summary(self, cust_id: Optional[int] = None) -> Dict:
        """Get the member stats summary from a certain cust_id

        Args:
            cust_id (int): The iRacing cust_id. Defaults to the authenticated member.

        Returns:
            dict: a dict containing the member stats summary

        """
        payload = {}
        if cust_id:
            payload = {"cust_id": cust_id}

        return self._get_resource("/data/stats/member_summary", payload=payload)

    def stats_member_yearly(self, cust_id: Optional[int] = None) -> Dict:
        """Get the member stats yearly from a certain cust_id

        Args:
            cust_id (int): The iRacing cust_id. Defaults to the authenticated member.

        Returns:
            dict: a dict containing the member stats yearly

        """
        payload = {}
        if cust_id:
            payload = {"cust_id": cust_id}

        return self._get_resource("/data/stats/member_yearly", payload=payload)

    def stats_season_driver_standings(
        self,
        season_id: int,
        car_class_id: int,
        race_week_num: Optional[int] = None,
        club_id: Optional[int] = None,
        division: Optional[int] = None,
    ) -> Dict:
        """Get the driver standings from a season.

        Args:
            season_id (int): The iRacing season id.
            car_class_id (int): the iRacing car class id.
            race_week_num (int): the race week number (0-12). Default 0.
            club_id (int): the iRacing club id. Defaults to all (-1).
            division (int): the iRacing division. Divisions are 0-based: 0 is Division 1, 10 is Rookie.
                            See /data/constants/divisons for more information. Defaults to all.

        Returns:
            dict: a dict containing the season driver standings

        """
        payload = {"season_id": season_id, "car_class_id": car_class_id}
        if race_week_num is not None:
            payload["race_week_num"] = race_week_num
        if club_id:
            payload["club_id"] = club_id
        if division is not None:
            payload["division"] = division

        resource = self._get_resource(
            "/data/stats/season_driver_standings", payload=payload
        )
        return self._get_chunks(resource.get("chunk_info"))

    def stats_season_supersession_standings(
        self,
        season_id: int,
        car_class_id: int,
        race_week_num: Optional[int] = None,
        club_id: Optional[int] = None,
        division: Optional[int] = None,
    ) -> Dict:
        """Get the supersession standings from a season.

        Args:
            season_id (int): The iRacing season id.
            car_class_id (int): the iRacing car class id.
            race_week_num (int): the race week number (0-12). Default 0.
            club_id (int): the iRacing club id. Defaults to all (-1).
            division (int): the iRacing division. Divisions are 0-based: 0 is Division 1, 10 is Rookie.
                            See /data/constants/divisons for more information. Defaults to all.

        Returns:
            dict: a dict containing the season supersession standings

        """
        payload = {"season_id": season_id, "car_class_id": car_class_id}
        if race_week_num is not None:
            payload["race_week_num"] = race_week_num
        if club_id:
            payload["club_id"] = club_id
        if division is not None:
            payload["division"] = division

        resource = self._get_resource(
            "/data/stats/season_supersession_standings", payload=payload
        )
        return self._get_chunks(resource.get("chunk_info"))

    def stats_season_team_standings(
        self, season_id: int, car_class_id: int, race_week_num: Optional[int] = None
    ) -> Dict:
        """Get the team standings from a season.

        Args:
            season_id (int): The iRacing season id.
            car_class_id (int): the iRacing car class id.
            race_week_num (int): the race week number (0-12).

        Returns:
            dict: a dict containing the season team standings

        """
        payload = {"season_id": season_id, "car_class_id": car_class_id}
        if race_week_num is not None:
            payload["race_week_num"] = race_week_num

        resource = self._get_resource(
            "/data/stats/season_team_standings", payload=payload
        )
        return self._get_chunks(resource.get("chunk_info"))

    def stats_season_tt_standings(
        self,
        season_id: int,
        car_class_id: int,
        race_week_num: Optional[int] = None,
        club_id: Optional[int] = None,
        division: Optional[int] = None,
    ) -> Dict:
        """Get the Time Trial standings from a season.

        Args:
            season_id (int): The iRacing season id.
            car_class_id (int): the iRacing car class id.
            race_week_num (int): the race week number (0-12).
            club_id (int): the iRacing club id.
            division (int): the iRacing division.

        Returns:
            dict: a dict containing the Time Trial standings

        """
        payload = {"season_id": season_id, "car_class_id": car_class_id}
        if race_week_num is not None:
            payload["race_week_num"] = race_week_num
        if club_id:
            payload["club_id"] = club_id
        if division is not None:
            payload["division"] = division

        resource = self._get_resource(
            "/data/stats/season_tt_standings", payload=payload
        )
        return self._get_chunks(resource.get("chunk_info"))

    def stats_season_tt_results(
        self,
        season_id: int,
        car_class_id: int,
        race_week_num: int,
        club_id: Optional[int] = None,
        division: Optional[int] = None,
    ) -> Dict:
        """Get the Time Trial results from a season.

        Args:
            season_id (int): The iRacing season id.
            car_class_id (int): the iRacing car class id.
            race_week_num (int): the race week number (0-12).
            club_id (int): the iRacing club id.
            division (int): the iRacing division.

        Returns:
            dict: a dict containing the Time Trial results

        """
        payload = {
            "season_id": season_id,
            "car_class_id": car_class_id,
            "race_week_num": race_week_num,
        }
        if club_id:
            payload["club_id"] = club_id
        if division is not None:
            payload["division"] = division

        resource = self._get_resource("/data/stats/season_tt_results", payload=payload)
        return self._get_chunks(resource.get("chunk_info"))

    def stats_season_qualify_results(
        self,
        season_id: int,
        car_class_id: int,
        race_week_num: int,
        club_id: Optional[int] = None,
        division: Optional[int] = None,
    ) -> Dict:
        """Get the qualifying results from a season.

        Args:
            season_id (int): The iRacing season id.
            car_class_id (int): the iRacing car class id.
            race_week_num (int): the race week number (0-12).
            club_id (int): the iRacing club id.
            division (int): the iRacing division.

        Returns:
            dict: a dict containing the qualifying results

        """
        payload = {
            "season_id": season_id,
            "car_class_id": car_class_id,
            "race_week_num": race_week_num,
        }
        if club_id:
            payload["club_id"] = club_id
        if division is not None:
            payload["division"] = division

        resource = self._get_resource(
            "/data/stats/season_qualify_results", payload=payload
        )
        return self._get_chunks(resource.get("chunk_info"))

    def stats_world_records(
        self,
        car_id: int,
        track_id: int,
        season_year: Optional[int] = None,
        season_quarter: Optional[int] = None,
    ) -> Dict:
        """Get the world records from a given track and car.

        Args:
            car_id (int): the iRacing car id
            track_id (int): the iRacing track id
            season_year (int): the season year
            season_quarter (int): the season quarter (1, 2, 3, 4). Only applicable when ``season_year`` is used.

        Returns:
            dict: a dict containing the world records

        """
        payload = {"car_id": car_id, "track_id": track_id}
        if season_year:
            payload["season_year"] = season_year
        if season_quarter:
            payload["season_quarter"] = season_quarter

        resource = self._get_resource("/data/stats/world_records", payload=payload)
        return self._get_chunks(resource.get("data", dict()).get("chunk_info"))

    def team(self, team_id: int, include_licenses: bool = False) -> Dict:
        """Get detailed team information.

        Args:
            team_id (int): the iRacing unique id from the team.
            include_licenses (bool): include licenses from all team members. For faster responses,
             only request when necesary.

        Returns:
            dict: a dict containing the team information.

        """
        payload = {"team_id": team_id, "include_licenses": include_licenses}
        return self._get_resource("/data/team/get", payload=payload)

    def season_list(self, season_year: int, season_quarter: int) -> Dict:
        """Get the list of iRacing Official seasons given a year and quarter.

        Args:
            season_year (int): the season year
            season_quarter (int): the season quarter (1, 2, 3, 4). Only applicable when ``season_year`` is used.

        Returns:
            dict: a dict containing the list of iRacing Official seasons given a year and quarter.

        """
        payload = {"season_year": season_year, "season_quarter": season_quarter}
        return self._get_resource("/data/season/list", payload=payload)

    def season_race_guide(
        self, start_from: str = None, include_end_after_from: bool = None
    ) -> Dict:
        """Get the season schedule race guide.

        Args:
            start_from (str): ISO-8601 offset format. Defaults to the current time.
             Include sessions with start times up to 3 hours after this time.
             Times in the past will be rewritten to the current time.
            include_end_after_from (bool): Include sessions which start before 'from' but end after.

        Returns:
            dict: a dict containing the season schedule race guide.

        """
        payload = {}
        if start_from:
            payload["from"] = start_from
        if include_end_after_from:
            payload["include_end_after_from"] = include_end_after_from

        return self._get_resource("/data/season/race_guide", payload=payload)

    def season_spectator_subsessionids(
        self, event_types: list[int] = [2, 3, 4, 5]
    ) -> list[int]:
        """Get the current list of subsession IDs for a given event type

        Args:
            event_types (list[int]): A list of integers that match with iRacing event types as follows:
                2: Practise
                3: Qualify
                4: Time Trial
                5: Race

        Returns:
            list: a list of the matching subsession IDs
        """
        payload = {}
        if event_types:
            payload["event_types"] = ",".join([str(x) for x in event_types])

        return self._get_resource(
            "/data/season/spectator_subsessionids", payload=payload
        )["subsession_ids"]

    def get_series(self) -> list[Dict]:
        """Get all the current official iRacing series.

        Returns:
            list: a list containing all the official iRacing series.

        """
        return self._get_resource("/data/series/get")

    def get_series_assets(self) -> Dict:
        """Get all the current official iRacing series assets.

        Get the images, description and logos from the current official iRacing series.
        Image paths are relative to https://images-static.iracing.com/

        Returns:
            dict: a dict containing all the current official iRacing series assets.

        """
        return self._get_resource("/data/series/assets")

    def series_past_seasons(self, series_id: int) -> Dict:
        """Get all seasons for a series.

        Filter list by ``'official'``: ``True`` for seasons with standings.

        Args:
            series_id ():

        Returns:
            dict: a dict containing information about the series and a list of seasons.
        """
        payload = {"series_id": series_id}
        return self._get_resource("/data/series/past_seasons", payload=payload).get(
            "series"
        )

    def series_seasons(self, include_series: bool = False) -> list[Dict]:
        """Get the all the seasons.

        To get series and seasons for which standings should be available, filter the list by ``'official'``: ``True``.

        Args:
            include_series (bool): whether if you want to include the series information or not. Default ``False``.

        Returns:
            list: a list containing all the series and seasons.

        """
        payload = {"include_series": include_series}
        return self._get_resource("/data/series/seasons", payload=payload)

    def series_stats(self) -> list[Dict]:
        """Get the all the series and seasons.

        To get series and seasons for which standings should be available, filter the list by ``'official'``: ``True``.

        Returns:
            list: a list containing all the series and seasons.

        """
        return self._get_resource("/data/series/stats_series")
