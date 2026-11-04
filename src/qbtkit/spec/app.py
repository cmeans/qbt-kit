"""The `app` scope — 16 endpoints on 5.2.3, 9 on 4.4.5.

Half of this scope is newer than the compatibility floor: cookies, API-key
management, directory browsing, process info and test email all arrived after
4.4.5, so each carries a `since` taken from the inventory diffs.
"""

from __future__ import annotations

from ._types import (
    Body,
    Endpoint,
    Json,
    NoContent,
    Param,
    RawText,
    Wire,
    flag,
    register,
    text,
)

# Writing any of these severs the caller's own connection or weakens the
# server's security posture. The MCP layer refuses them outright and the CLI
# warns; a model nudging `web_ui_password` would lock the user out of their own
# client.
LOCKOUT_KEYS = frozenset(
    {
        "web_ui_username",
        "web_ui_password",
        "web_ui_port",
        "web_ui_address",
        "web_ui_domain_list",
        "use_https",
        "bypass_local_auth",
        "bypass_auth_subnet_whitelist",
        "bypass_auth_subnet_whitelist_enabled",
        "web_ui_host_header_validation_enabled",
        "web_ui_csrf_protection_enabled",
        "web_ui_clickjacking_protection_enabled",
        "web_ui_secure_cookie_enabled",
        "web_ui_max_auth_fail_count",
        "web_ui_ban_duration",
        "alternative_webui_enabled",
        "alternative_webui_path",
        # Reachability: UPnP forwards the WebUI off the LAN, and a reverse-proxy
        # trust list lets a forged X-Forwarded-For defeat IP bans and the subnet
        # whitelist.
        "web_ui_upnp",
        "web_ui_reverse_proxy_enabled",
        "web_ui_reverse_proxies_list",
        # Pointing HTTPS at a missing or unreadable file locks you out as surely
        # as changing the port.
        "web_ui_https_cert_path",
        "web_ui_https_key_path",
        "web_ui_custom_http_headers",
        "web_ui_use_custom_http_headers_enabled",
        # Arbitrary command execution on torrent events. This is the single
        # worst thing an agent could be talked into doing through this API, and
        # nothing legitimate about managing torrents needs to write it.
        "autorun_enabled",
        "autorun_program",
        "autorun_on_torrent_added_enabled",
        "autorun_on_torrent_added_program",
        # The same outcome by a second route, missed when autorun_program was
        # blocked: qBittorrent runs this binary for every plugin search, so
        # pointing it at any executable already on the host and then searching
        # runs that file. Reported externally against 0.1.2.
        "python_executable_path",
    }
)

# Keys a language model may write. Everything else is refused on the agent path
# and warned about on the CLI.
#
# This is an allowlist because the blocklist above, which is careful and well
# reasoned, still missed three routes to code execution and mass data deletion.
# A blocklist has to anticipate every dangerous key, including ones qBittorrent
# has not shipped yet; an allowlist only has to recognise the harmless ones, and
# fails closed when a new key appears.
#
# The exclusions follow principles rather than a list. Not here, deliberately:
# anything naming a path the *server* writes (`export_dir*`, `file_log_path`,
# `scan_dirs`); anything naming a host, URL or program (`proxy_*`, `dyndns_*`,
# `mail_notification_*`, `i2p_*`, `announce_ip`); anything changing who is
# trusted (`add_trackers*`, `ignore_ssl_errors`, `ssrf_mitigation`,
# `validate_https_tracker_certificate`, `embedded_tracker*`); the share-limit
# *action* and its triggers, because `max_ratio_act` 1 and 3 delete every
# finished torrent's data and the user may already have set it; and
# `current_network_interface`/`current_interface_address`, which silently stop
# all transfers.
SAFE_PREF_KEYS = frozenset(
    {
        # Rate limits and the scheduler.
        "dl_limit",
        "up_limit",
        "alt_dl_limit",
        "alt_up_limit",
        "bittorrent_protocol",
        "limit_lan_peers",
        "limit_tcp_overhead",
        "limit_utp_rate",
        "scheduler_enabled",
        "scheduler_days",
        "schedule_from_hour",
        "schedule_from_min",
        "schedule_to_hour",
        "schedule_to_min",
        # Queueing.
        "queueing_enabled",
        "max_active_downloads",
        "max_active_torrents",
        "max_active_uploads",
        "max_active_checking_torrents",
        "dont_count_slow_torrents",
        "slow_torrent_dl_rate_threshold",
        "slow_torrent_ul_rate_threshold",
        "slow_torrent_inactive_timer",
        # Paths the user already owns, and how content is laid out.
        "save_path",
        "temp_path",
        "temp_path_enabled",
        "auto_tmm_enabled",
        "torrent_changed_tmm_enabled",
        "save_path_changed_tmm_enabled",
        "category_changed_tmm_enabled",
        "use_subcategories",
        "torrent_content_layout",
        "add_stopped_enabled",
        "start_paused_enabled",
        "preallocate_all",
        "incomplete_files_ext",
        "auto_delete_mode",
        "recheck_completed_torrents",
        "export_torrent_dir_disabled",
        # Connectivity that is not the WebUI.
        "listen_port",
        "random_port",
        "upnp",
        "dht",
        "pex",
        "lsd",
        "encryption",
        "anonymous_mode",
        "max_connec",
        "max_connec_per_torrent",
        "max_uploads",
        "max_uploads_per_torrent",
        "utp_tcp_mixed_mode",
        "outgoing_ports_min",
        "outgoing_ports_max",
        "enable_multi_connections_from_same_ip",
        # RSS, toggles and intervals only; feed URLs live in the rss scope.
        "rss_processing_enabled",
        "rss_refresh_interval",
        "rss_max_articles_per_feed",
        "rss_auto_downloading_enabled",
        "rss_download_repack_proper_episodes",
        # libtorrent performance knobs.
        "async_io_threads",
        "hashing_threads",
        "file_pool_size",
        "checking_memory_use",
        "disk_cache",
        "disk_cache_ttl",
        "disk_queue_size",
        "disk_io_type",
        "disk_io_read_mode",
        "disk_io_write_mode",
        "send_buffer_watermark",
        "send_buffer_low_watermark",
        "send_buffer_watermark_factor",
        "socket_backlog_size",
        "socket_send_buffer_size",
        "socket_receive_buffer_size",
        "connection_speed",
        "peer_turnover",
        "peer_turnover_cutoff",
        "peer_turnover_interval",
        "request_queue_size",
        "upload_slots_behavior",
        "upload_choking_algorithm",
        "enable_coalesce_read_write",
        "enable_piece_extent_affinity",
        "enable_upload_suggestions",
        "announce_to_all_tiers",
        "announce_to_all_trackers",
        "stop_tracker_timeout",
        "max_concurrent_http_announces",
        "reannounce_when_address_changed",
        "idn_support_enabled",
        "resume_data_storage_type",
        "refresh_interval",
        "resolve_peer_countries",
    }
)

register(
    Endpoint(
        scope="app",
        name="version",
        method="GET",
        summary="qBittorrent's own version, e.g. v5.2.3.",
        returns=RawText,
    ),
    Endpoint(
        scope="app",
        name="webapiVersion",
        method="GET",
        summary="WebAPI version, e.g. 2.15.1. Requires a session: measured 403 "
        "unauthenticated on every tested version, so this cannot be used "
        "to decide how to authenticate.",
        returns=RawText,
    ),
    Endpoint(
        scope="app",
        name="buildInfo",
        method="GET",
        since=(2, 3, 0),
        summary="Qt, libtorrent, boost, OpenSSL and zlib versions.",
        returns=Json("BuildInfo"),
    ),
    Endpoint(
        scope="app",
        name="defaultSavePath",
        method="GET",
        summary="The configured default save path.",
        returns=RawText,
    ),
    Endpoint(
        scope="app",
        name="preferences",
        method="GET",
        summary="All application preferences as one nested object.",
        returns=Json("Preferences"),
    ),
    Endpoint(
        scope="app",
        name="setPreferences",
        method="POST",
        summary="Write preferences. Takes a JSON object in the `json` form field; "
        "unknown keys pass through untouched rather than being dropped.",
        returns=NoContent,
        body=Body.URLENCODED,
        params=(
            Param(
                "json",
                Wire.JSON_BLOB,
                required=True,
                summary="object of preference keys to set",
                blocklist=LOCKOUT_KEYS,
                allowlist=SAFE_PREF_KEYS,
            ),
        ),
        mutating=True,
    ),
    Endpoint(
        scope="app",
        name="shutdown",
        method="POST",
        summary="Shut qBittorrent down. There is no corresponding start.",
        returns=NoContent,
        mutating=True,
        destructive=True,
        untestable="terminates the server under test",
    ),
    # ----- newer than the floor
    Endpoint(
        scope="app",
        name="getDirectoryContent",
        method="GET",
        since=(2, 9, 3),
        summary="List a directory on the server's filesystem. Useful for "
        "validating a save path before setting it.",
        returns=Json("DirectoryEntry"),
        params=(text("dirPath", required=True), flag("withSymlinks")),
    ),
    Endpoint(
        scope="app",
        name="processInfo",
        method="GET",
        since=(2, 11, 3),
        summary="Process statistics. On 5.2.3 this is just `launch_time` — "
        "measured, not assumed from the field name.",
        returns=Json("ProcessInfo"),
    ),
    Endpoint(
        scope="app",
        name="cookies",
        method="GET",
        since=(2, 11, 2),
        summary="Cookies qBittorrent sends when fetching .torrent URLs.",
        returns=Json("Cookie"),
        # The whole reply is credentials — private-tracker session cookies — and
        # nothing an agent does with torrents needs to read them. Forbidding
        # beats redacting: a cookie's secret is in a field called `value`, and
        # redacting by that name is both too broad elsewhere and too narrow here.
        agent_forbidden=True,
    ),
    Endpoint(
        scope="app",
        name="setCookies",
        method="POST",
        since=(2, 11, 2),
        summary="Replace the stored cookie list. JSON in a form field.",
        returns=NoContent,
        body=Body.URLENCODED,
        params=(Param("cookies", Wire.JSON_BLOB, required=True),),
        mutating=True,
        # "Replace" means the user's private-tracker cookies are gone, which is
        # this codebase's own definition of destructive, and it could instead
        # plant a cookie sent to every .torrent URL qBittorrent fetches.
        agent_forbidden=True,
    ),
    Endpoint(
        scope="app",
        name="rotateAPIKey",
        method="POST",
        since=(2, 15, 0),
        summary='Generate a new API key, returned as {"apiKey": …}. Invalidates '
        "the previous key, so a client authenticating with Bearer will "
        "lose access until it stores the new one.",
        returns=Json("ApiKey"),
        mutating=True,
        destructive=True,
    ),
    Endpoint(
        scope="app",
        name="deleteAPIKey",
        method="POST",
        since=(2, 15, 0),
        summary="Delete the API key, disabling Bearer authentication.",
        returns=NoContent,
        mutating=True,
        destructive=True,
    ),
    Endpoint(
        scope="app",
        name="sendTestEmail",
        method="POST",
        since=(2, 10, 4),
        summary="Send a test notification email.",
        returns=NoContent,
        mutating=True,
        untestable="needs an SMTP server",
    ),
    Endpoint(
        scope="app",
        name="networkInterfaceList",
        method="GET",
        since=(2, 3, 0),
        summary="Network interfaces qBittorrent can bind to.",
        returns=Json("NetworkInterface"),
    ),
    Endpoint(
        scope="app",
        name="networkInterfaceAddressList",
        method="GET",
        since=(2, 3, 0),
        summary="Addresses available on one interface.",
        returns=Json("InterfaceAddress"),
        params=(text("iface"),),
    ),
)
