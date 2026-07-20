using System.Text.Json.Serialization;

namespace MatchZy;
public class MatchZyEvent
{
    public MatchZyEvent(string eventName)
    {
        EventName = eventName;
    }

    [JsonPropertyName("event")]
    public string EventName { get; }
}

public class MatchZyMatchEvent : MatchZyEvent
{
    [JsonPropertyName("matchid")]
    public required long MatchId { get; init; }

    protected MatchZyMatchEvent(string eventName) : base(eventName)
    {
    }
}

public class MatchZyMatchTeamEvent : MatchZyMatchEvent
{
    [JsonPropertyName("team")]
    public required string Team { get; init; }

    protected MatchZyMatchTeamEvent(string eventName) : base(eventName)
    {
    }
}

public class MatchZyMapEvent : MatchZyMatchEvent
{
    [JsonPropertyName("map_number")]
    public required int MapNumber { get; init; }

    protected MatchZyMapEvent(string eventName) : base(eventName)
    {
    }
}

public class MatchZyMapTeamEvent : MatchZyMapEvent
{
    [JsonPropertyName("team_int")]
    public required int TeamNumber { get; init; }

    protected MatchZyMapTeamEvent(string eventName) : base(eventName)
    {
    }
}

public class MatchZyRoundEvent : MatchZyMapEvent
{
    [JsonPropertyName("round_number")]
    public required int RoundNumber { get; init; }

    protected MatchZyRoundEvent(string eventName) : base(eventName)
    {
    }
}

public class MatchZyTimedRoundEvent : MatchZyRoundEvent
{
    [JsonPropertyName("round_time")]
    public required int RoundTime { get; init; }

    protected MatchZyTimedRoundEvent(string eventName) : base(eventName)
    {
    }
}

public class MatchZyPlayerRoundEvent : MatchZyRoundEvent
{

    [JsonPropertyName("player")]
    public required int Player { get; init; }

    protected MatchZyPlayerRoundEvent(string eventName) : base(eventName)
    {
    }
}

public class MatchZyPlayerTimedRoundEvent : MatchZyTimedRoundEvent
{
    [JsonPropertyName("player")]
    public required int Player { get; init; }

    protected MatchZyPlayerTimedRoundEvent(string eventName) : base(eventName)
    {
    }
}

public class MatchZyPlayerConnectedEvent : MatchZyMatchEvent
{
    [JsonPropertyName("player")]
    public required MatchZyPlayerInfo Player { get; init; }

    public MatchZyPlayerConnectedEvent() : base("player_connect")
    {
    }
}

public class MatchZyPlayerDisconnectedEvent : MatchZyMatchEvent
{
    [JsonPropertyName("player")]
    public required MatchZyPlayerInfo Player { get; init; }

    public MatchZyPlayerDisconnectedEvent() : base("player_disconnect")
    {
    }
}

public class MatchZyBackupLoadedEvent : MatchZyMapEvent
{
    [JsonPropertyName("round_number")]
    public required int RoundNumber { get; init; }

    [JsonPropertyName("filename")]
    public required string FileName { get; init; }

    public MatchZyBackupLoadedEvent() : base("backup_loaded")
    {
    }
}

public class MatchZySeriesStartedEvent : MatchZyMatchEvent
{
    [JsonPropertyName("team1")]
    public required MatchZyTeamWrapper Team1 { get; init; }

    [JsonPropertyName("team2")]
    public required MatchZyTeamWrapper Team2 { get; init; }

    [JsonPropertyName("num_maps")]
    public required int NumberOfMaps { get; init; }

    public MatchZySeriesStartedEvent() : base("series_start")
    {
    }
}

public class MatchZySeriesResultEvent : MatchZyMatchEvent
{
    [JsonPropertyName("time_until_restore")]
    public required int TimeUntilRestore { get; init; }

    [JsonPropertyName("winner")]
    public required Winner Winner { get; init; }

    [JsonPropertyName("team1_series_score")]
    public required int Team1SeriesScore { get; init; }

    [JsonPropertyName("team2_series_score")]
    public required int Team2SeriesScore { get; init; }

    public MatchZySeriesResultEvent() : base("series_end")
    {
    }
}

public class GoingLiveEvent : MatchZyMapEvent
{
    public GoingLiveEvent() : base("going_live")
    {
    }
}

public class MatchZyRoundEndedEvent : MatchZyTimedRoundEvent
{

    [JsonPropertyName("reason")]
    public required int Reason { get; init; }

    [JsonPropertyName("winner")]
    public required Winner Winner { get; init; }

    [JsonPropertyName("team1")]
    public required MatchZyStatsTeam StatsTeam1 { get; init; }

    [JsonPropertyName("team2")]
    public required MatchZyStatsTeam StatsTeam2 { get; init; }

    public MatchZyRoundEndedEvent() : base("round_end")
    {
    }
}

public class MapResultEvent : MatchZyMapEvent
{
    [JsonPropertyName("winner")]
    public required Winner Winner { get; init; }

    [JsonPropertyName("team1")]
    public required MatchZyStatsTeam StatsTeam1 { get; init; }

    [JsonPropertyName("team2")]
    public required MatchZyStatsTeam StatsTeam2 { get; init; }

    public MapResultEvent() : base("map_result")
    {
    }
}

public class MatchZyDemoUploadedEvent : MatchZyMatchEvent
{
    [JsonPropertyName("map_number")]
    public required int MapNumber { get; init; }

    [JsonPropertyName("filename")]
    public required string FileName { get; init; }

    [JsonPropertyName("success")]
    public bool Success { get; set; }

    public MatchZyDemoUploadedEvent() : base("demo_upload_ended")
    {
    }
}

// ============================================================================
// Demo lifecycle events (recording + upload progress)
// ============================================================================

public class MatchZyDemoRecordingStartedEvent : MatchZyMapEvent
{
    [JsonPropertyName("filename")]
    public required string FileName { get; init; }

    public MatchZyDemoRecordingStartedEvent() : base("demo_recording_start")
    {
    }
}

public class MatchZyDemoRecordingStoppedEvent : MatchZyMapEvent
{
    [JsonPropertyName("filename")]
    public required string FileName { get; init; }

    public MatchZyDemoRecordingStoppedEvent() : base("demo_recording_stop")
    {
    }
}

public class MatchZyDemoUploadStartedEvent : MatchZyMapEvent
{
    [JsonPropertyName("filename")]
    public required string FileName { get; init; }

    [JsonPropertyName("size_mb")]
    public required double SizeMB { get; init; }

    public MatchZyDemoUploadStartedEvent() : base("demo_upload_start")
    {
    }
}

public class MatchZyDemoUploadSuccessEvent : MatchZyMapEvent
{
    [JsonPropertyName("filename")]
    public required string FileName { get; init; }

    [JsonPropertyName("size_mb")]
    public required double SizeMB { get; init; }

    [JsonPropertyName("status")]
    public required string Status { get; init; }

    public MatchZyDemoUploadSuccessEvent() : base("demo_upload_success")
    {
    }
}

public class MatchZyDemoUploadFailEvent : MatchZyMapEvent
{
    [JsonPropertyName("filename")]
    public required string FileName { get; init; }

    [JsonPropertyName("size_mb")]
    public double? SizeMB { get; init; }

    [JsonPropertyName("status")]
    public required string Status { get; init; }

    [JsonPropertyName("reason")]
    public required string Reason { get; init; }

    public MatchZyDemoUploadFailEvent() : base("demo_upload_fail")
    {
    }
}

// Player Ready System Events
public class MatchZyPlayerReadyEvent : MatchZyMatchEvent
{
    [JsonPropertyName("player")]
    public required MatchZyPlayerInfo Player { get; init; }

    [JsonPropertyName("team")]
    public required string Team { get; init; }

    [JsonPropertyName("ready_count_team1")]
    public required int ReadyCountTeam1 { get; init; }

    [JsonPropertyName("ready_count_team2")]
    public required int ReadyCountTeam2 { get; init; }

    [JsonPropertyName("total_ready")]
    public required int TotalReady { get; init; }

    [JsonPropertyName("expected_total")]
    public required int ExpectedTotal { get; init; }

    public MatchZyPlayerReadyEvent() : base("player_ready")
    {
    }
}

public class MatchZyPlayerUnreadyEvent : MatchZyMatchEvent
{
    [JsonPropertyName("player")]
    public required MatchZyPlayerInfo Player { get; init; }

    [JsonPropertyName("team")]
    public required string Team { get; init; }

    [JsonPropertyName("ready_count_team1")]
    public required int ReadyCountTeam1 { get; init; }

    [JsonPropertyName("ready_count_team2")]
    public required int ReadyCountTeam2 { get; init; }

    [JsonPropertyName("total_ready")]
    public required int TotalReady { get; init; }

    [JsonPropertyName("expected_total")]
    public required int ExpectedTotal { get; init; }

    public MatchZyPlayerUnreadyEvent() : base("player_unready")
    {
    }
}

public class MatchZyTeamReadyEvent : MatchZyMatchEvent
{
    [JsonPropertyName("team")]
    public required string Team { get; init; }

    [JsonPropertyName("ready_count")]
    public required int ReadyCount { get; init; }

    [JsonPropertyName("total_ready")]
    public required int TotalReady { get; init; }

    [JsonPropertyName("expected_total")]
    public required int ExpectedTotal { get; init; }

    public MatchZyTeamReadyEvent() : base("team_ready")
    {
    }
}

public class MatchZyAllPlayersReadyEvent : MatchZyMatchEvent
{
    [JsonPropertyName("ready_count_team1")]
    public required int ReadyCountTeam1 { get; init; }

    [JsonPropertyName("ready_count_team2")]
    public required int ReadyCountTeam2 { get; init; }

    [JsonPropertyName("total_ready")]
    public required int TotalReady { get; init; }

    [JsonPropertyName("countdown_started")]
    public required bool CountdownStarted { get; init; }

    public MatchZyAllPlayersReadyEvent() : base("all_players_ready")
    {
    }
}

// Match Phase Change Events
public class MatchZyWarmupEndedEvent : MatchZyMapEvent
{
    public MatchZyWarmupEndedEvent() : base("warmup_ended")
    {
    }
}

public class MatchZyKnifeRoundStartedEvent : MatchZyMapEvent
{
    public MatchZyKnifeRoundStartedEvent() : base("knife_round_started")
    {
    }
}

public class MatchZyKnifeRoundEndedEvent : MatchZyMapEvent
{
    [JsonPropertyName("winner")]
    public required string Winner { get; init; }

    public MatchZyKnifeRoundEndedEvent() : base("knife_round_ended")
    {
    }
}

// Pause System Events
public class MatchZyPauseRequestedEvent : MatchZyMapEvent
{
    [JsonPropertyName("requested_by")]
    public required MatchZyPlayerInfo RequestedBy { get; init; }

    [JsonPropertyName("is_tactical")]
    public required bool IsTactical { get; init; }

    [JsonPropertyName("is_admin")]
    public required bool IsAdmin { get; init; }

    public MatchZyPauseRequestedEvent() : base("pause_requested")
    {
    }
}

public class MatchZyMatchPausedEvent : MatchZyMapEvent
{
    [JsonPropertyName("paused_by")]
    public required MatchZyPlayerInfo PausedBy { get; init; }

    [JsonPropertyName("is_tactical")]
    public required bool IsTactical { get; init; }

    [JsonPropertyName("is_admin")]
    public required bool IsAdmin { get; init; }

    [JsonPropertyName("pause_time")]
    public required long PauseTime { get; init; }

    public MatchZyMatchPausedEvent() : base("match_paused")
    {
    }
}

public class MatchZyUnpauseRequestedEvent : MatchZyMapEvent
{
    [JsonPropertyName("team")]
    public required string Team { get; init; }

    [JsonPropertyName("teams_ready")]
    public required int TeamsReady { get; init; }

    [JsonPropertyName("teams_needed")]
    public required int TeamsNeeded { get; init; }

    public MatchZyUnpauseRequestedEvent() : base("unpause_requested")
    {
    }
}

public class MatchZyMatchUnpausedEvent : MatchZyMapEvent
{
    [JsonPropertyName("pause_duration")]
    public required long PauseDuration { get; init; }

    public MatchZyMatchUnpausedEvent() : base("match_unpaused")
    {
    }
}

// Round and Game State Events
public class MatchZyRoundStartedEvent : MatchZyRoundEvent
{
    [JsonPropertyName("team1_score")]
    public required int Team1Score { get; init; }

    [JsonPropertyName("team2_score")]
    public required int Team2Score { get; init; }

    public MatchZyRoundStartedEvent() : base("round_started")
    {
    }
}

public class MatchZyHalftimeStartedEvent : MatchZyMapEvent
{
    [JsonPropertyName("team1_score")]
    public required int Team1Score { get; init; }

    [JsonPropertyName("team2_score")]
    public required int Team2Score { get; init; }

    public MatchZyHalftimeStartedEvent() : base("halftime_started")
    {
    }
}

public class MatchZyOvertimeStartedEvent : MatchZyMapEvent
{
    [JsonPropertyName("overtime_number")]
    public required int OvertimeNumber { get; init; }

    public MatchZyOvertimeStartedEvent() : base("overtime_started")
    {
    }
}

public class MatchZySideSwapEvent : MatchZyMapEvent
{
    [JsonPropertyName("team1_side")]
    public required string Team1Side { get; init; }

    [JsonPropertyName("team2_side")]
    public required string Team2Side { get; init; }

    public MatchZySideSwapEvent() : base("side_swap")
    {
    }
}

// Test Event
public class MatchZyServerConfiguredEvent : MatchZyEvent
{
    [JsonPropertyName("server_id")]
    public required string ServerId { get; init; }

    [JsonPropertyName("hostname")]
    public required string Hostname { get; init; }

    [JsonPropertyName("plugin_version")]
    public required string PluginVersion { get; init; }

    [JsonPropertyName("remote_log_url")]
    public required string RemoteLogUrl { get; init; }

    [JsonPropertyName("timestamp")]
    public required long Timestamp { get; init; }

    [JsonPropertyName("configured_by")]
    public required string ConfiguredBy { get; init; }

    public MatchZyServerConfiguredEvent() : base("server_configured")
    {
    }
}

public class MatchZyServerHealthEvent : MatchZyEvent
{
    [JsonPropertyName("server_id")]
    public required string ServerId { get; init; }

    [JsonPropertyName("plugin_version")]
    public required string PluginVersion { get; init; }

    [JsonPropertyName("timestamp")]
    public required long Timestamp { get; init; }

    [JsonPropertyName("db_ok")]
    public required bool DbOk { get; init; }

    [JsonPropertyName("db_type")]
    public required string DbType { get; init; } // "sqlite" | "mysql"

    [JsonPropertyName("db_error")]
    public string? DbError { get; init; }

    [JsonPropertyName("reason")]
    public string? Reason { get; init; } // "startup" | "periodic" | "change"

    public MatchZyServerHealthEvent() : base("server_health")
    {
    }
}

public class MatchZyTestEvent : MatchZyMatchEvent
{
    [JsonPropertyName("message")]
    public required string Message { get; init; }

    [JsonPropertyName("timestamp")]
    public required long Timestamp { get; init; }

    [JsonPropertyName("triggered_by")]
    public required string TriggeredBy { get; init; }

    // Optional metadata to help external controllers attribute connectivity checks
    // cleanly per server and per logical match slug.
    [JsonPropertyName("server_id")]
    public string? ServerId { get; init; }

    [JsonPropertyName("match_slug")]
    public string? MatchSlug { get; init; }

    public MatchZyTestEvent() : base("test_event")
    {
    }
}

// CS2 update status events (server-level)
public class MatchZyCs2UpdateRequiredEvent : MatchZyMatchEvent
{
    [JsonPropertyName("server_id")]
    public required string ServerId { get; init; }

    [JsonPropertyName("required_version")]
    public required int RequiredVersion { get; init; }

    // Best-effort marker so the API/admin UI can distinguish "update found"
    // vs "about to quit now".
    [JsonPropertyName("phase")]
    public string? Phase { get; init; }

    [JsonPropertyName("timestamp")]
    public required long Timestamp { get; init; }

    public MatchZyCs2UpdateRequiredEvent() : base("cs2_update_required")
    {
    }
}

using System.Text.Json;
using CounterStrikeSharp.API;
using CounterStrikeSharp.API.Core;
using CounterStrikeSharp.API.Core.Attributes.Registration;
using CounterStrikeSharp.API.Modules.Commands;
using CounterStrikeSharp.API.Modules.Cvars;
using CounterStrikeSharp.API.Modules.Utils;
using Newtonsoft.Json.Linq;


namespace MatchZy
{

    public partial class MatchZy
    {
        public MatchConfig matchConfig = new();

        public bool isMatchSetup = false;

        // When a loadmatch_url request is issued while a match is still in postgame,
        // we queue the next match here and auto-load it after ResetMatch() completes.
        private bool isMatchQueued = false;
        private string? queuedMatchUrl;
        private string? queuedMatchHeaderName;
        private string? queuedMatchHeaderValue;

        public bool matchModeOnly = false;

        public bool resetCvarsOnSeriesEnd = true;

        public string loadedConfigFile = "";

        public Team matchzyTeam1 = new()
        {
            teamName = "COUNTER-TERRORISTS"
        };
        public Team matchzyTeam2 = new()
        {
            teamName = "TERRORISTS"
        };

        public Dictionary<Team, string> teamSides = new();
        public Dictionary<string, Team> reverseTeamSides = new();

        [ConsoleCommand("css_team1", "Sets team name for team1")]
        public void OnTeam1Command(CCSPlayerController? player, CommandInfo command)
        {
            HandleTeamNameChangeCommand(player, command.ArgString, 1);
        }

        [ConsoleCommand("css_team2", "Sets team name for team2")]
        public void OnTeam2Command(CCSPlayerController? player, CommandInfo command)
        {
            HandleTeamNameChangeCommand(player, command.ArgString, 2);
        }

        [ConsoleCommand("matchzy_loadmatch", "Loads a match from the given JSON file path (relative to the csgo/ directory)")]
        public void LoadMatch(CCSPlayerController? player, CommandInfo command)
        {
            try
            {
                if (player != null) return;
                if (isMatchSetup)
                {
                    // command.ReplyToCommand($"[LoadMatch] A match is already setup with id: {liveMatchId}, cannot load a new match!");
                    ReplyToUserCommand(player, Localizer["matchzy.mm.matchisalreadysetup", liveMatchId]);
                    Log($"[LoadMatch] A match is already setup with id: {liveMatchId}, cannot load a new match!");
                    return;
                }
                string fileName = command.ArgString;
                string filePath = Path.Join(Server.GameDirectory + "/csgo", fileName);
                if (!File.Exists(filePath))
                {
                    // command.ReplyToCommand($"[LoadMatch] Provided file does not exist! Usage: matchzy_loadmatch <filename>");
                    ReplyToUserCommand(player, Localizer["matchzy.mm.filedoesntexist"]);
                    Log($"[LoadMatch] Provided file does not exist! Usage: matchzy_loadmatch <filename>");
                    return;
                }
                string jsonData = File.ReadAllText(filePath);
                bool success = LoadMatchFromJSON(jsonData);
                if (!success)
                {
                    // command.ReplyToCommand("Match load failed! Resetting current match");
                    ReplyToUserCommand(player, Localizer["matchzy.mm.matchloadfailed"]);
                    UpdateTournamentStatus("error");
                    ResetMatch();
                }
                loadedConfigFile = fileName;
            }
            catch (Exception e)
            {
                Log($"[LoadMatch - FATAL] An error occured: {e.Message}");
                UpdateTournamentStatus("error");
                return;
            }
        }

        [ConsoleCommand("get5_loadmatch_url", "Loads a match from the given URL")]
        [ConsoleCommand("matchzy_loadmatch_url", "Loads a match from the given URL")]
        public void LoadMatchFromURL(CCSPlayerController? player, CommandInfo command)
        {
            if (player != null) return;
            string url = command.ArgByIndex(1);

            string headerName = command.ArgCount > 3 ? command.ArgByIndex(2) : "";
            string headerValue = command.ArgCount > 3 ? command.ArgByIndex(3) : "";

            // If a match is already setup, allow queuing the next match only once the current
            // series has reached the postgame phase. The queued match will be auto-loaded
            // from ResetMatch() after demo upload and cleanup finishes.
            if (isMatchSetup)
            {
                string currentStatus = tournamentStatus.Value ?? string.Empty;
                if (string.Equals(currentStatus, "postgame", StringComparison.OrdinalIgnoreCase))
                {
                    // Heuristically derive a slug/identifier for the queued match from the URL, if possible.
                    string nextMatchIdentifier = url;
                    try
                    {
                        if (Uri.TryCreate(url, UriKind.Absolute, out var uri))
                        {
                            var path = uri.AbsolutePath.TrimEnd('/');
                            var lastSegment = path.Split('/', StringSplitOptions.RemoveEmptyEntries).LastOrDefault();
                            if (!string.IsNullOrEmpty(lastSegment))
                            {
                                nextMatchIdentifier = System.IO.Path.GetFileNameWithoutExtension(lastSegment);
                            }
                        }
                    }
                    catch
                    {
                        // Fallback: keep the raw URL as identifier.
                        nextMatchIdentifier = url;
                    }

                    queuedMatchUrl = url;
                    queuedMatchHeaderName = headerName;
                    queuedMatchHeaderValue = headerValue;
                    isMatchQueued = true;

                    Log($"[LoadMatchDataCommand] Current match {liveMatchId} is in postgame. Queuing next match from URL: {url} to load after reset.");
                    ReplyToUserCommand(player, $"[LoadMatchDataCommand] Current match {liveMatchId} is finishing. Queued next match from URL: {url} to load after reset.");

                    // Surface this state to the allocator / UI so it knows a new match is lined up.
                    UpdateTournamentStatus("queued");
                    tournamentNextMatch.Value = nextMatchIdentifier;
                }
                else
                {
                    // command.ReplyToCommand($"[LoadMatchDataCommand] A match is already setup with id: {liveMatchId}, cannot load a new match!");
                    ReplyToUserCommand(player, Localizer["matchzy.mm.get5matchisalreadysetup", liveMatchId]);
                    Log($"[LoadMatchDataCommand] A match is already setup with id: {liveMatchId}, cannot load a new match! (status={currentStatus})");
                }
                return;
            }

            Log($"[LoadMatchDataCommand] Match setup request received with URL: {url} headerName: {headerName} and headerValue: {headerValue}");

            if (!IsValidUrl(url))
            {
                // command.ReplyToCommand($"[LoadMatchDataCommand] Invalid URL: {url}. Please provide a valid URL to load the match!");
                ReplyToUserCommand(player, Localizer["matchzy.mm.invalidurl", url]);
                Log($"[LoadMatchDataCommand] Invalid URL: {url}. Please provide a valid URL to load the match!");
                return;
            }
            try
            {
                HttpClient httpClient = new();
                if (headerName != "")
                {
                    httpClient.DefaultRequestHeaders.Add(headerName, headerValue);
                }
                HttpResponseMessage response = httpClient.GetAsync(url).Result;

                if (response.IsSuccessStatusCode)
                {
                    string jsonData = response.Content.ReadAsStringAsync().Result;
                    Log($"[LoadMatchFromURL] Received following data: {jsonData}");

                    bool success = LoadMatchFromJSON(jsonData);
                    if (!success)
                    {
                        // command.ReplyToCommand("Match load failed! Resetting current match");
                        ReplyToUserCommand(player, Localizer["matchzy.mm.matchloadfailed"]);
                        UpdateTournamentStatus("error");
                        ResetMatch();
                    }
                    loadedConfigFile = url;
                }
                else
                {
                    // command.ReplyToCommand($"[LoadMatchFromURL] HTTP request failed with status code: {response.StatusCode}");
                    ReplyToUserCommand(player, Localizer["matchzy.mm.httprequestfailed", response.StatusCode]);
                    UpdateTournamentStatus("error");
                    Log($"[LoadMatchFromURL] HTTP request failed with status code: {response.StatusCode}");
                }
            }
            catch (Exception e)
            {
                Log($"[LoadMatchFromURL - FATAL] An error occured: {e.Message}");
                UpdateTournamentStatus("error");
                return;
            }
        }

        /// <summary>
        /// If a match was queued while the previous series was still active (postgame),
        /// load it now that ResetMatch() has completed and the server is idle again.
        /// </summary>
        private void TryLoadQueuedMatchAfterReset()
        {
            if (!isMatchQueued || string.IsNullOrWhiteSpace(queuedMatchUrl))
            {
                return;
            }

            string url = queuedMatchUrl!;
            string? headerName = queuedMatchHeaderName;
            string? headerValue = queuedMatchHeaderValue;

            // Clear the queue state up-front so we don't accidentally loop if something fails.
            isMatchQueued = false;
            queuedMatchUrl = null;
            queuedMatchHeaderName = null;
            queuedMatchHeaderValue = null;
            tournamentNextMatch.Value = "";

            Log($"[MatchQueue] Attempting to auto-load queued match from URL after reset: {url}");

            try
            {
                HttpClient httpClient = new();
                if (!string.IsNullOrEmpty(headerName))
                {
                    httpClient.DefaultRequestHeaders.Add(headerName, headerValue);
                }

                HttpResponseMessage response = httpClient.GetAsync(url).Result;

                if (response.IsSuccessStatusCode)
                {
                    string jsonData = response.Content.ReadAsStringAsync().Result;
                    Log($"[LoadQueuedMatch] Received following data for queued match: {jsonData}");

                    bool success = LoadMatchFromJSON(jsonData);
                    if (!success)
                    {
                        Log("[LoadQueuedMatch] Queued match load failed. Keeping server in idle/error state.");
                        UpdateTournamentStatus("error");
                    }
                    else
                    {
                        loadedConfigFile = url;
                    }
                }
                else
                {
                    Log($"[LoadQueuedMatch] HTTP request for queued match failed with status code: {response.StatusCode}");
                    UpdateTournamentStatus("error");
                }
            }
            catch (Exception e)
            {
                Log($"[LoadQueuedMatch - FATAL] An error occured while loading queued match: {e.Message}");
                UpdateTournamentStatus("error");
            }
        }

        static string ValidateMatchJsonStructure(JObject jsonData)
        {
            string[] requiredFields = { "maplist", "team1", "team2", "num_maps" };

            // Check if any required field is missing
            foreach (string field in requiredFields)
            {
                if (jsonData[field] == null)
                {
                    return $"Missing mandatory field: {field}";
                }
            }

            foreach (var property in jsonData.Properties())
            {
                string field = property.Name;

                switch (field)
                {
                    case "matchid":
                    case "players_per_team":
                    case "min_players_to_ready":
                    case "min_spectators_to_ready":
                    case "num_maps":
                        int numMaps;
                        if (!int.TryParse(jsonData[field]!.ToString(), out numMaps))
                        {
                            return $"{field} should be an integer!";

                        }
                        if (field == "num_maps" && numMaps > jsonData["maplist"]!.ToObject<List<string>>()!.Count)
                        {
                            return $"{field} should be equal to or greater than maplist!";
                        }

                        break;

                    case "cvars":
                        if (jsonData[field]!.Type != JTokenType.Object)
                        {
                            return $"{field} should be a JSON structure!";
                        }
                        break;

                    case "team1":
                    case "team2":
                    case "spectators":
                        if (jsonData[field]!.Type != JTokenType.Object)
                        {
                            return $"{field} should be a JSON structure!";
                        }
                        if ((field != "spectators") && (jsonData[field]!["players"] == null || jsonData[field]!["players"]!.Type != JTokenType.Object))
                        {
                            return $"{field} should have 'players' JSON!";
                        }
                        break;

                    case "veto_mode":
                        if (jsonData[field]!.Type != JTokenType.Array)
                        {
                            return $"{field} should be an Array!";
                        }
                        break;

                    case "maplist":
                        if (jsonData[field]!.Type != JTokenType.Array)
                        {
                            return $"{field} should be an Array!";
                        }
                        if (!jsonData[field]!.Any())
                        {
                            return $"{field} should contain atleast 1 map!";
                        }

                        break;
                    case "map_sides":
                        if (jsonData[field]!.Type != JTokenType.Array)
                        {
                            return $"{field} should be an Array!";
                        }
                        string[] allowedValues = { "team1_ct", "team1_t", "team2_ct", "team2_t", "knife" };
                        bool allElementsValid = jsonData[field]!.All(element => allowedValues.Contains(element.ToString()));

                        if (!allElementsValid)
                        {
                            return $"{field} should be \"team1_ct\", \"team1_t\", or \"knife\"!";
                        }

                        if (jsonData[field]!.ToObject<List<string>>()!.Count < jsonData["num_maps"]!.Value<int>())
                        {
                            return $"{field} should be equal to or greater than num_maps!";
                        }
                        break;

                    case "skip_veto":
                    case "clinch_series":
                    case "wingman":
                        if (!bool.TryParse(jsonData[field]!.ToString(), out bool result))
                        {
                            return $"{field} should be a boolean!";
                        }
                        break;
                }
            }

            return "";
        }

        public bool LoadMatchFromJSON(string jsonData)
        {

            JObject jsonDataObject = JObject.Parse(jsonData);

            string validationError = ValidateMatchJsonStructure(jsonDataObject);

            if (validationError != "")
            {
                Log($"[LoadMatchDataCommand] {validationError}");
                UpdateTournamentStatus("error");
                return false;
            }

            if (jsonDataObject["matchid"] != null)
            {
                liveMatchId = (long)jsonDataObject["matchid"]!;
            }

            // Update tournament status to loading with match ID
            UpdateTournamentStatus("loading", liveMatchId.ToString());
            JToken team1 = jsonDataObject["team1"]!;
            JToken team2 = jsonDataObject["team2"]!;
            JToken maplist = jsonDataObject["maplist"]!;

            if (team1["id"] != null) matchzyTeam1.id = team1["id"]!.ToString();
            if (team2["id"] != null) matchzyTeam2.id = team2["id"]!.ToString();

            matchzyTeam1.teamName = RemoveSpecialCharacters(team1["name"]!.ToString());
            matchzyTeam2.teamName = RemoveSpecialCharacters(team2["name"]!.ToString());
            matchzyTeam1.teamPlayers = team1["players"];
            matchzyTeam2.teamPlayers = team2["players"];

            // Preserve any externally-configured remote log settings across match loads so that
            // an outside controller (MatchZy Auto Tournament) does not need to constantly
            // reapply them after every matchzy_loadmatch_url call.
            string previousRemoteLogUrl = matchConfig.RemoteLogURL;
            string previousRemoteLogHeaderKey = matchConfig.RemoteLogHeaderKey;
            string previousRemoteLogHeaderValue = matchConfig.RemoteLogHeaderValue;

            matchConfig = new()
            {
                MatchId = liveMatchId,
                MapsPool = maplist.ToObject<List<string>>()!,
                MapsLeftInVetoPool = maplist.ToObject<List<string>>()!,
                NumMaps = jsonDataObject["num_maps"]!.Value<int>(),
                MinPlayersToReady = minimumReadyRequired,
                RemoteLogURL = previousRemoteLogUrl,
                RemoteLogHeaderKey = previousRemoteLogHeaderKey,
                RemoteLogHeaderValue = previousRemoteLogHeaderValue
            };

            GetOptionalMatchValues(jsonDataObject);

            // If the JSON payload explicitly provides a remote_log_url and/or headers, treat
            // that as an authoritative configuration as well.
            if (jsonDataObject["remote_log_url"] != null)
            {
                string? remoteUrl = jsonDataObject["remote_log_url"]!.ToString();
                if (!string.IsNullOrWhiteSpace(remoteUrl) && IsValidUrl(remoteUrl))
                {
                    matchConfig.RemoteLogURL = remoteUrl;
                    remoteLogUrlEverConfigured = true;
                    remoteLogUrlMissingWarningLogged = false;
                }
            }
            if (jsonDataObject["remote_log_header_key"] != null)
            {
                matchConfig.RemoteLogHeaderKey = jsonDataObject["remote_log_header_key"]!.ToString();
            }
            if (jsonDataObject["remote_log_header_value"] != null)
            {
                matchConfig.RemoteLogHeaderValue = jsonDataObject["remote_log_header_value"]!.ToString();
            }

            // Track whether this match should be run in bot-driven simulation mode.
            isSimulationMode = matchConfig.Simulation;

            if (isSimulationMode)
            {
                // Validate that we actually have configured players for simulation.
                bool team1HasPlayers = matchzyTeam1.teamPlayers is JObject t1 && t1.Properties().Any();
                bool team2HasPlayers = matchzyTeam2.teamPlayers is JObject t2 && t2.Properties().Any();

                if (!team1HasPlayers && !team2HasPlayers)
                {
                    Log("[LOADMATCH] Simulation requested but no players configured for team1/team2. Aborting match load.");
                    UpdateTournamentStatus("error");
                    return false;
                }
            }

            // Validate that we have enough maps to play the series.
            if (matchConfig.MapsPool.Count < matchConfig.NumMaps)
            {
                Log($"[LOADMATCH] The map pool {matchConfig.MapsPool.Count} is not large enough to play a series of {matchConfig.NumMaps} maps.");
                return false;
            }

            GetCvarValues(jsonDataObject);

            Log($"[LOADMATCH] MinPlayersToReady: {matchConfig.MinPlayersToReady} SeriesClinch: {matchConfig.SeriesCanClinch}");
            Log($"[LOADMATCH] MapsPool: {string.Join(", ", matchConfig.MapsPool)} MapsLeftInVetoPool: {string.Join(", ", matchConfig.MapsLeftInVetoPool)}");

            LoadClientNames();

            // Build the final maplist deterministically from the configured pool.
            matchConfig.Maplist.Clear();
            for (int i = 0; i < matchConfig.NumMaps; i++)
            {
                matchConfig.Maplist.Add(matchConfig.MapsPool[i]);

                // Ensure there is a side entry per map.
                if (matchConfig.MapSides.Count < matchConfig.Maplist.Count)
                {
                    if (matchConfig.MatchSideType == "standard" || matchConfig.MatchSideType == "always_knife")
                    {
                        matchConfig.MapSides.Add("knife");
                    }
                    else if (matchConfig.MatchSideType == "random")
                    {
                        matchConfig.MapSides.Add(new Random().Next(0, 2) == 0 ? "team1_ct" : "team1_t");
                    }
                    else
                    {
                        matchConfig.MapSides.Add("team1_ct");
                    }
                }
            }

            string currentMapName = Server.MapName;
            string mapName = matchConfig.Maplist[0];

            bool willChangeMap = IsMapReloadRequiredForGameMode(matchConfig.Wingman) || mapReloadRequired || currentMapName != mapName;

            if (willChangeMap)
            {
                SetCorrectGameMode();
                ChangeMap(mapName, 0);

                // In simulation mode we don't want to spawn bots on the *old* map right
                // before a changelevel, as they will immediately be kicked. Instead we
                // defer the simulation flow until after the new map is fully loaded.
                if (isSimulationMode)
                {
                    simulationFlowDeferred = true;
                    simulationTargetMap = mapName;
                }
            }

            readyAvailable = true;

            // This is done before starting warmup so that cvars like get5_remote_log_url are set properly to send the events
            ExecuteChangedConvars();

            StartWarmup();

            isMatchSetup = true;
            
            // Auto-ready simulation helper: when enabled, spawn two bots (1 CT + 1 T) after warmup
            // has started so auto-ready/ready gating can be tested without a human joining.
            // We delay slightly so warmup.cfg + humans.cfg (bot_kick) have already executed.
            ScheduleAutoReadySimulationFlowIfNeeded(2.0f);

            // If this match is configured for simulation and we are *not* in the middle of a
            // map change, schedule the simulation flow shortly after warmup is active so the
            // server is fully ready to accept connections (just like in a human match).
            if (isSimulationMode && !simulationFlowDeferred)
            {
                ScheduleSimulationFlowStart(5.0f);
            }

            SetMapSides();

            SetTeamNames();
            UpdatePlayersMap();
            UpdateHostname();

            // If auto-ready is enabled, check if players already on teams should be auto-readied
            if (autoReadyEnabled.Value && readyAvailable && !matchStarted)
            {
                // Use a small delay to ensure all team assignments are complete
                AddTimer(1.0f, () => {
                    CheckAndAutoReadyPlayers();
                });
            }

            var seriesStartedEvent = new MatchZySeriesStartedEvent
            {
                MatchId = liveMatchId,
                NumberOfMaps = matchConfig.NumMaps,
                Team1 = new(matchzyTeam1.id, matchzyTeam1.teamName),
                Team2 = new(matchzyTeam2.id, matchzyTeam2.teamName),
            };

            Task.Run(async () =>
            {
                await SendEventAsync(seriesStartedEvent);
            });

            Log($"[LoadMatchFromJSON] Success with matchid: {liveMatchId}!");
            return true;
        }

        public void SetMapSides()
        {
            int mapNumber = matchConfig.CurrentMapNumber;
            if (matchConfig.MapSides[mapNumber] == "team1_ct" || matchConfig.MapSides[mapNumber] == "team2_t")
            {
                teamSides[matchzyTeam1] = "CT";
                teamSides[matchzyTeam2] = "TERRORIST";
                reverseTeamSides["CT"] = matchzyTeam1;
                reverseTeamSides["TERRORIST"] = matchzyTeam2;
                isKnifeRequired = false;
            }
            else if (matchConfig.MapSides[mapNumber] == "team2_ct" || matchConfig.MapSides[mapNumber] == "team1_t")
            {
                teamSides[matchzyTeam2] = "CT";
                teamSides[matchzyTeam1] = "TERRORIST";
                reverseTeamSides["CT"] = matchzyTeam2;
                reverseTeamSides["TERRORIST"] = matchzyTeam1;
                isKnifeRequired = false;
            }
            else if (matchConfig.MapSides[mapNumber] == "knife")
            {
                isKnifeRequired = true;
            }

            SetTeamNames();
        }

        public void SetTeamNames()
        {
            Server.ExecuteCommand($"mp_teamname_1 {reverseTeamSides["CT"].teamName}");
            Server.ExecuteCommand($"mp_teamname_2 {reverseTeamSides["TERRORIST"].teamName}");
        }

        public void GetCvarValues(JObject jsonDataObject)
        {
            try
            {
                if (jsonDataObject["cvars"] == null) return;

                foreach (JProperty cvarData in jsonDataObject["cvars"]!)
                {
                    string cvarName = cvarData.Name;
                    string cvarValue = cvarData.Value.ToString();

                    var cvar = ConVar.Find(cvarName);
                    matchConfig.ChangedCvars[cvarName] = cvarValue;
                    if (cvar != null)
                    {
                        matchConfig.OriginalCvars[cvarName] = GetConvarStringValue(cvar);
                    }
                }

            }
            catch (Exception e)
            {
                Log($"[GetCvarValues FATAL] An error occurred: {e.Message}");
            }
        }

        public void GetOptionalMatchValues(JObject jsonDataObject)
        {
            // Map and roster/spectator configuration
            if (jsonDataObject["map_sides"] != null)
            {
                matchConfig.MapSides = jsonDataObject["map_sides"]!.ToObject<List<string>>()!;
            }
            if (jsonDataObject["players_per_team"] != null)
            {
                matchConfig.PlayersPerTeam = jsonDataObject["players_per_team"]!.Value<int>();
            }
            if (jsonDataObject["min_players_to_ready"] != null)
            {
                matchConfig.MinPlayersToReady = jsonDataObject["min_players_to_ready"]!.Value<int>();
            }
            if (jsonDataObject["min_spectators_to_ready"] != null)
            {
                matchConfig.MinSpectatorsToReady = jsonDataObject["min_spectators_to_ready"]!.Value<int>();
            }
            if (jsonDataObject["spectators"] != null && jsonDataObject["spectators"]!["players"] != null)
            {
                matchConfig.Spectators = jsonDataObject["spectators"]!["players"]!;
                if (matchConfig.Spectators is JArray spectatorsArray && spectatorsArray.Count == 0)
                {
                    // Convert the empty JArray to an empty JObject
                    matchConfig.Spectators = new JObject();
                }
            }

            // Optional: per-match admin SteamIDs. When present, these Steam64 IDs are treated
            // as admins for the duration of this match in addition to any global admins from
            // CSSharp or MatchZy admins.json.
            if (jsonDataObject["admins"] != null)
            {
                try
                {
                    var adminIds = jsonDataObject["admins"]!.ToObject<List<string>>() ?? new List<string>();
                    matchConfig.AdminSteamIds = adminIds;
                }
                catch (Exception)
                {
                    Log("[LOADMATCH] Invalid admins list in JSON; ignoring per-match admins.");
                    matchConfig.AdminSteamIds = new List<string>();
                }
            }

            // Series / flow toggles
            if (jsonDataObject["clinch_series"] != null)
            {
                matchConfig.SeriesCanClinch = bool.Parse(jsonDataObject["clinch_series"]!.ToString());
            }
            if (jsonDataObject["skip_veto"] != null)
            {
                matchConfig.SkipVeto = bool.Parse(jsonDataObject["skip_veto"]!.ToString());
            }
            if (jsonDataObject["wingman"] != null)
            {
                matchConfig.Wingman = bool.Parse(jsonDataObject["wingman"]!.ToString());
            }

            // Simulation / practice controls
            if (jsonDataObject["simulation"] != null)
            {
                matchConfig.Simulation = bool.Parse(jsonDataObject["simulation"]!.ToString());
            }
            if (jsonDataObject["simulation_timescale"] != null)
            {
                // Allow either numeric or string; clamp to a safe range so we don't accidentally
                // set something extreme like 0 or 100x speed.
                try
                {
                    float ts = jsonDataObject["simulation_timescale"]!.Value<float>();
                    if (float.IsNaN(ts) || float.IsInfinity(ts)) throw new Exception("NaN/Inf");

                    // Clamp between 0.1x and 4x to avoid crazy values.
                    if (ts < 0.1f) ts = 0.1f;
                    if (ts > 4.0f) ts = 4.0f;

                    matchConfig.SimulationTimeScale = ts;
                }
                catch (Exception)
                {
                    Log("[LOADMATCH] Invalid simulation_timescale value in JSON; falling back to default 1.0");
                    matchConfig.SimulationTimeScale = 1.0f;
                }
            }

            // Veto / map selection mode
            if (jsonDataObject["veto_mode"] != null)
            {
                matchConfig.MapBanOrder = jsonDataObject["veto_mode"]!.ToObject<List<string>>()!;
            }

            // Tournament overtime / regulation configuration (shuffle tournaments, etc.).
            // These are advisory match-level settings that we translate into the
            // appropriate CS2 cvars when the match goes live.
            if (jsonDataObject["maxRounds"] != null)
            {
                try
                {
                    matchConfig.MaxRounds = jsonDataObject["maxRounds"]!.Value<int>();
                    Log($"[GetOptionalMatchValues] Parsed maxRounds={matchConfig.MaxRounds}");
                }
                catch (Exception e)
                {
                    Log($"[GetOptionalMatchValues] Failed to parse maxRounds: {e.Message}");
                }
            }

            if (jsonDataObject["overtimeMode"] != null)
            {
                try
                {
                    matchConfig.OvertimeMode = jsonDataObject["overtimeMode"]!.ToString();
                    Log($"[GetOptionalMatchValues] Parsed overtimeMode='{matchConfig.OvertimeMode}'");
                }
                catch (Exception e)
                {
                    Log($"[GetOptionalMatchValues] Failed to parse overtimeMode: {e.Message}");
                }
            }

            if (jsonDataObject["overtimeSegments"] != null)
            {
                try
                {
                    matchConfig.OvertimeSegments = jsonDataObject["overtimeSegments"]!.Value<int>();
                    Log($"[GetOptionalMatchValues] Parsed overtimeSegments={matchConfig.OvertimeSegments}");
                }
                catch (Exception e)
                {
                    Log($"[GetOptionalMatchValues] Failed to parse overtimeSegments: {e.Message}");
                }
            }
        }

        public void HandleTeamNameChangeCommand(CCSPlayerController? player, string teamName, int teamNum)
        {
            if (!IsPlayerAdmin(player, "css_team", "@css/config"))
            {
                SendPlayerNotAdminMessage(player);
                return;
            }
            if (matchStarted)
            {
                // ReplyToUserCommand(player, "Team names cannot be changed once the match is started!");
                ReplyToUserCommand(player, Localizer["matchzy.mm.teamcannotbechanged"]);
                return;
            }
            teamName = RemoveSpecialCharacters(teamName.Trim());
            if (teamName == "")
            {
                // ReplyToUserCommand(player, $"Usage: !team{teamNum} <name>");
                ReplyToUserCommand(player, Localizer["matchzy.cc.usage", $"!team{teamNum} <name>"]);
            }

            if (teamNum == 1)
            {
                matchzyTeam1.teamName = teamName;
                teamSides[matchzyTeam1] = "CT";
                reverseTeamSides["CT"] = matchzyTeam1;
                foreach (var coach in matchzyTeam1.coach)
                {
                    coach.Clan = $"[{matchzyTeam1.teamName} COACH]";
                }
            }
            else if (teamNum == 2)
            {
                matchzyTeam2.teamName = teamName;
                teamSides[matchzyTeam2] = "TERRORIST";
                reverseTeamSides["TERRORIST"] = matchzyTeam2;
                foreach (var coach in matchzyTeam2.coach)
                {
                    coach.Clan = $"[{matchzyTeam2.teamName} COACH]";
                }
            }
            Server.ExecuteCommand($"mp_teamname_{teamNum} {teamName};");
        }

        public void SwapSidesInTeamData(bool swapTeams)
        {
            // if (swapTeams) {
            //     // Here, we sync matchzyTeam1 and matchzyTeam2 with the actual team1 and team2
            //     (matchzyTeam2, matchzyTeam1) = (matchzyTeam1, matchzyTeam2);
            // }

            (teamSides[matchzyTeam1], teamSides[matchzyTeam2]) = (teamSides[matchzyTeam2], teamSides[matchzyTeam1]);
            (reverseTeamSides["CT"], reverseTeamSides["TERRORIST"]) = (reverseTeamSides["TERRORIST"], reverseTeamSides["CT"]);

            // Send side_swap event
            if (isMatchLive)
            {
                Log($"[SwapSidesInTeamData] Sending side_swap event");

                var sideSwapEvent = new MatchZySideSwapEvent
                {
                    MatchId = liveMatchId,
                    MapNumber = matchConfig.CurrentMapNumber,
                    Team1Side = teamSides[matchzyTeam1],
                    Team2Side = teamSides[matchzyTeam2]
                };

                Task.Run(async () =>
                {
                    await SendEventAsync(sideSwapEvent);
                });

                // Check if this is halftime or overtime
                var gameRules = Utilities.FindAllEntitiesByDesignerName<CCSGameRulesProxy>("cs_gamerules").First().GameRules!;
                int roundsPlayed = gameRules.TotalRoundsPlayed;
                int roundsPerHalf = ConVar.Find("mp_maxrounds")!.GetPrimitiveValue<int>() / 2;
                int roundsPerOTHalf = ConVar.Find("mp_overtime_maxrounds")!.GetPrimitiveValue<int>() / 2;

                if (roundsPlayed == roundsPerHalf)
                {
                    // This is halftime
                    Log($"[SwapSidesInTeamData] Halftime detected, sending halftime_started event");
                    (int t1score, int t2score) = GetTeamsScore();
                    var halftimeStartedEvent = new MatchZyHalftimeStartedEvent
                    {
                        MatchId = liveMatchId,
                        MapNumber = matchConfig.CurrentMapNumber,
                        Team1Score = t1score,
                        Team2Score = t2score
                    };

                    Task.Run(async () =>
                    {
                        await SendEventAsync(halftimeStartedEvent);
                    });
                    UpdateTournamentStatus("halftime");
                }
                else if (roundsPlayed >= 2 * roundsPerHalf)
                {
                    // This is overtime
                    int otround = roundsPlayed - 2 * roundsPerHalf;
                    if ((otround + roundsPerOTHalf) % (2 * roundsPerOTHalf) == 0)
                    {
                        int overtimeNumber = (otround / (2 * roundsPerOTHalf)) + 1;
                        Log($"[SwapSidesInTeamData] Overtime detected, sending overtime_started event - OT#{overtimeNumber}");

                        var overtimeStartedEvent = new MatchZyOvertimeStartedEvent
                        {
                            MatchId = liveMatchId,
                            MapNumber = matchConfig.CurrentMapNumber,
                            OvertimeNumber = overtimeNumber
                        };

                        Task.Run(async () =>
                        {
                            await SendEventAsync(overtimeStartedEvent);
                        });
                    }
                }
            }
        }

        private CsTeam GetPlayerTeam(CCSPlayerController player)
        {
            CsTeam playerTeam = CsTeam.None;
            var steamId = player.SteamID;
            try
            {
                if (matchzyTeam1.teamPlayers != null && matchzyTeam1.teamPlayers[steamId.ToString()] != null)
                {
                    if (teamSides[matchzyTeam1] == "CT")
                    {
                        playerTeam = CsTeam.CounterTerrorist;
                    }
                    else if (teamSides[matchzyTeam1] == "TERRORIST")
                    {
                        playerTeam = CsTeam.Terrorist;
                    }

                }
                else if (matchzyTeam2.teamPlayers != null && matchzyTeam2.teamPlayers[steamId.ToString()] != null)
                {
                    if (teamSides[matchzyTeam2] == "CT")
                    {
                        playerTeam = CsTeam.CounterTerrorist;
                    }
                    else if (teamSides[matchzyTeam2] == "TERRORIST")
                    {
                        playerTeam = CsTeam.Terrorist;
                    }
                }
                else if (matchConfig.Spectators != null && matchConfig.Spectators[steamId.ToString()] != null)
                {
                    playerTeam = CsTeam.Spectator;
                }
            }
            catch (Exception ex)
            {
                Log($"[GetPlayerTeam - FATAL] Exception occurred: {ex.Message}");
            }
            return playerTeam;
        }

        public void EndSeries(string? winnerName, int restartDelay, int t1score, int t2score)
        {
            long matchId = liveMatchId;
            (int team1Score, int team2Score) = (matchzyTeam1.seriesScore, matchzyTeam2.seriesScore);
            Log($"[SeriesCheckpoint] SERIES END reached for match {matchId}. Final map score: {matchzyTeam1.teamName} {t1score} – {matchzyTeam2.teamName} {t2score}. Final series score: {matchzyTeam1.teamName} {team1Score} – {matchzyTeam2.teamName} {team2Score}.");
            if (winnerName == null)
            {
                PrintToAllChat($"{ChatColors.Green}{matchzyTeam1.teamName}{ChatColors.Default} and {ChatColors.Green}{matchzyTeam2.teamName}{ChatColors.Default} have tied the match");
            }
            else
            {
                Server.PrintToChatAll($"{chatPrefix} {ChatColors.Green}{winnerName}{ChatColors.Default} has won the match");
            }

            string winnerTeam = (winnerName == null) ? "none" : matchzyTeam1.seriesScore > matchzyTeam2.seriesScore ? "team1" : "team2";

            var seriesResultEvent = new MatchZySeriesResultEvent()
            {
                MatchId = matchId,
                Winner = new Winner(t1score > t2score && reverseTeamSides["CT"] == matchzyTeam1 ? "3" : "2", winnerTeam),
                Team1SeriesScore = team1Score,
                Team2SeriesScore = team2Score,
                TimeUntilRestore = 10,
            };

            Task.Run(async () =>
            {
                await database.SetMatchEndData(matchId, winnerName ?? "Draw", team1Score, team2Score);
                // Making sure that map end event is fired first
                await Task.Delay(2000);
                await SendEventAsync(seriesResultEvent);
            });

            if (resetCvarsOnSeriesEnd) ResetChangedConvars();
            isMatchLive = false;

            // In simulation mode, schedule bots to disconnect gradually after the series ends
            // and restore normal server behavior (timescale/cheats) for subsequent human matches.
            if (isSimulationMode)
            {
                ScheduleSimulationBotDisconnects();
                Log("[SimulationMode] Series ended - resetting host_timescale to 1 and sv_cheats to 0.");
                Server.ExecuteCommand("host_timescale 1; sv_cheats 0");
            }

            // Calculate kick delay based on demo recording and upload configuration
            bool hasUploadEndpoint = !string.IsNullOrEmpty(demoUploadURL);
            int kickDelay;
            
            if (!isDemoRecordingEnabled)
            {
                // Demo recording disabled - very fast
                kickDelay = restartDelay + seriesEndKickDelayNoDemo.Value;
            }
            else if (!hasUploadEndpoint)
            {
                // Demo recording enabled but no upload URL - don't wait for upload that won't happen
                kickDelay = restartDelay + seriesEndKickDelayDemoNoUpload.Value;
            }
            else
            {
                // Demo recording enabled with upload URL - wait for upload to complete
                kickDelay = restartDelay + seriesEndKickDelayDemoUpload.Value;
            }
            
            Log($"[EndSeries] Demo recording: {isDemoRecordingEnabled}, Upload URL: {hasUploadEndpoint}, kickDelay: {kickDelay}s");

            int minutes = kickDelay / 60;
            int seconds = kickDelay % 60;
            string timeText = minutes > 0 ? $"{minutes} minute{(minutes > 1 ? "s" : "")} {seconds} second{(seconds > 1 ? "s" : "")}" : $"{seconds} second{(seconds > 1 ? "s" : "")}";

            string resetMessage;
            if (!isDemoRecordingEnabled)
            {
                resetMessage = $"{ChatColors.Grey}Series ended. Server will reset in {ChatColors.Yellow}{timeText}{ChatColors.Default}.";
            }
            else if (!hasUploadEndpoint)
            {
                resetMessage = $"{ChatColors.Grey}Series ended. Server will reset in {ChatColors.Yellow}{timeText}{ChatColors.Default} after demo is saved.";
            }
            else
            {
                resetMessage = $"{ChatColors.Grey}Series ended. Server will reset in {ChatColors.Yellow}{timeText}{ChatColors.Default} after demo upload completes.";
            }
            
            PrintToAllChat(resetMessage);
            PrintToAllChat($"{ChatColors.Grey}All players will be disconnected to prepare the server for the next match.{ChatColors.Default}");

            // Show countdown on center screen for last 30 seconds (or full duration if less than 30s)
            int countdownStart = kickDelay > 30 ? 30 : kickDelay;
            if (kickDelay > 30)
            {
                // Start countdown from 30 seconds mark
                AddTimer(kickDelay - 30, () =>
                {
                    StartCountdown(30, "⚠️ SERVER RESTART<br>{0}s", "#ff0000");
                });
            }
            else if (kickDelay > 0)
            {
                // Start countdown immediately if less than 30 seconds
                StartCountdown(kickDelay, "⚠️ SERVER RESTART<br>{0}s", "#ff0000");
            }

            AddTimer(kickDelay, () =>
            {
                // Kick all players with winner message
                Log("[EndSeries] Kicking all players after series end...");
                var playerEntities = Utilities.FindAllEntitiesByDesignerName<CCSPlayerController>("cs_player_controller");
                int kickedCount = 0;
                
                // Build kick reason message
                string? kickReason = null;
                if (!string.IsNullOrEmpty(winnerName))
                {
                    kickReason = Localizer["matchzy.match.won", winnerName];
                }
                
                foreach (var player in playerEntities)
                {
                    if (player == null) continue;
                    if (!player.IsValid || player.IsBot || player.IsHLTV) continue;
                    if (player.Connected != PlayerConnectedState.PlayerConnected) continue;

                    if (player.UserId.HasValue)
                    {
                        Log($"[EndSeries] Kicking player: {player.PlayerName} (UserId: {player.UserId}, reason: {kickReason ?? "none"})");
                        KickPlayer(player, kickReason);
                        kickedCount++;
                    }
                }
                Log($"[EndSeries] Kicked {kickedCount} players. Resetting match...");

                // Update status to idle to indicate server is ready for allocation
                UpdateTournamentStatus("idle", "");

                // Reset match after a short delay to ensure kicks are processed
                AddTimer(2.0f, () =>
                {
                    ResetMatch(false);
                });
            });
        }

        public void HandlePlayoutConfig()
        {
            if (isPlayOutEnabled)
            {
                Server.ExecuteCommand("mp_overtime_enable 0");
                Server.ExecuteCommand("mp_match_can_clinch false");
            }
            else
            {
                var absoluteCfgPath = Path.Join(Server.GameDirectory + "/csgo/cfg", GetGameMode() == 1 ? liveCfgPath : liveWingmanCfgPath);
                string? matchCanClinch = GetConvarValueFromCFGFile(absoluteCfgPath, "mp_match_can_clinch");
                Server.ExecuteCommand($"mp_match_can_clinch {matchCanClinch ?? "1"}");

                // If the JSON explicitly specified an overtimeMode, that is the single
                // source of truth here. Re-assert it after the live.cfg/base cfg has
                // been applied so that nothing can silently re-enable overtime.
                if (!string.IsNullOrWhiteSpace(matchConfig.OvertimeMode))
                {
                    string mode = matchConfig.OvertimeMode!.ToLowerInvariant();
                    if (mode == "enabled")
                    {
                        Log("[OvertimeConfig] HandlePlayoutConfig confirming overtime enabled from match config.");
                        Server.ExecuteCommand("mp_overtime_enable 1");
                    }
                    else if (mode == "disabled")
                    {
                        Log("[OvertimeConfig] HandlePlayoutConfig confirming overtime disabled from match config.");
                        Server.ExecuteCommand("mp_overtime_enable 0");
                    }
                }
                else
                {
                    // No JSON override – fall back to whatever the base cfg says.
                    string? overtimeEnabled = GetConvarValueFromCFGFile(absoluteCfgPath, "mp_overtime_enable");
                    Server.ExecuteCommand($"mp_overtime_enable {overtimeEnabled ?? "1"}");
                }
            }
        }

    }
}


