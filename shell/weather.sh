# Source AFTER starship init; compatible with Bash and Zsh on Linux/macOS.

_starship_weather_clear() {
    unset STARSHIP_WEATHER_BLUE STARSHIP_WEATHER_GREEN
    unset STARSHIP_WEATHER_YELLOW STARSHIP_WEATHER_PINK
}

# The private subshell keeps its coprocess/FDs out of the user's shell.
# Zsh sysread bounds the whole read, including an incomplete output line.
# This uses builtins on macOS too; GNU timeout is not required.
_starship_weather_read() (
    local _sw_pid=0 _sw_payload='' _sw_chunk='' _sw_result=1 _sw_read_status
    if [ -n "${ZSH_VERSION:-}" ]; then
        zmodload zsh/system 2>/dev/null || return 1
        zmodload zsh/datetime 2>/dev/null || return 1
        local -F _sw_deadline _sw_remaining
        _sw_deadline=$((EPOCHREALTIME + 0.5))
        coproc { exec uv "$@" </dev/null 2>/dev/null; }
        _sw_pid=$!
        exec 9<&p
        while true; do
            _sw_remaining=$((_sw_deadline - EPOCHREALTIME))
            (( _sw_remaining > 0 )) || break
            sysread -i 9 -t "$_sw_remaining" -s 256 _sw_chunk
            _sw_read_status=$?
            case "$_sw_read_status" in
                0) _sw_payload="$_sw_payload$_sw_chunk" ;;
                5) _sw_result=0; break ;; # EOF: the producer closed stdout.
                *) break ;;              # Timeout or failed read.
            esac
            [ "${#_sw_payload}" -le 64 ] || break
        done
    else
        exec 9< <(exec uv "$@" </dev/null 2>/dev/null)
        _sw_pid=$!
        local _sw_timeout=0.5
        # Legacy Bash (e.g. macOS /bin/bash) accepts integer timeouts only.
        [ "${BASH_VERSINFO[0]}" -ge 4 ] || _sw_timeout=1
        IFS= read -r -t "$_sw_timeout" _sw_payload <&9 && _sw_result=0
    fi
    exec 9<&-
    if [ "$_sw_result" -ne 0 ]; then
        # Never signal PID 0 (the process group). A stuck uv is optional.
        if [ "$_sw_pid" -gt 0 ]; then
            kill -KILL "$_sw_pid" 2>/dev/null
            wait "$_sw_pid" 2>/dev/null
        fi
        return 1
    fi
    printf '%s\n' "$_sw_payload"
)

_starship_weather_update() {
    local _sw_root _sw_payload _sw_band _sw_icon
    if ! command -v uv >/dev/null 2>&1; then
        _starship_weather_clear
        return 0
    fi
    # SECONDS is a shell builtin in both Bash and Zsh; no date subprocess.
    if [ -n "${_STARSHIP_WEATHER_LAST_POLL:-}" ] &&
        [ "$((SECONDS - _STARSHIP_WEATHER_LAST_POLL))" -ge 0 ] &&
        [ "$((SECONDS - _STARSHIP_WEATHER_LAST_POLL))" -lt 5 ]; then
        return 0
    fi
    _STARSHIP_WEATHER_LAST_POLL=$SECONDS
    _starship_weather_clear
    _sw_root="${STARSHIP_CONFIG:-$HOME/.config/starship/starship.toml}"
    _sw_root="${_sw_root%/*}"
    [ -r "$_sw_root/scripts/weather.py" ] || return 0
    # Explicit version and no-project/no-config isolate this from the cwd.
    # No interpreter downloads, package installs, or HTTP in the prompt path.
    _sw_payload="$(_starship_weather_read run --no-project --no-config --no-python-downloads \
        --offline --python '>=3.10' "$_sw_root/scripts/weather.py" prompt 2>/dev/null)" || return 0
    IFS=$'\t' read -r _sw_band _sw_icon <<< "$_sw_payload"
    case "$_sw_band" in
        blue)   export STARSHIP_WEATHER_BLUE="$_sw_icon" ;;
        green)  export STARSHIP_WEATHER_GREEN="$_sw_icon" ;;
        yellow) export STARSHIP_WEATHER_YELLOW="$_sw_icon" ;;
        pink)   export STARSHIP_WEATHER_PINK="$_sw_icon" ;;
    esac
    return 0
}

if [ -n "${ZSH_VERSION:-}" ]; then
    _starship_weather_zsh_precmd() {
        local _sw_status=$?
        _starship_weather_update
        return "$_sw_status"
    }
    autoload -Uz add-zsh-hook
    add-zsh-hook -d precmd _starship_weather_zsh_precmd 2>/dev/null
    add-zsh-hook precmd _starship_weather_zsh_precmd
elif [ -n "${BASH_VERSION:-}" ]; then
    if [ "${starship_precmd_user_func:-}" != '_starship_weather_bash_precmd' ]; then
        _STARSHIP_WEATHER_PREVIOUS_BASH_PRECMD="${starship_precmd_user_func:-}"
    fi
    _starship_weather_bash_precmd() {
        local _sw_status=$?
        if [ -n "${_STARSHIP_WEATHER_PREVIOUS_BASH_PRECMD:-}" ] &&
            declare -F "$_STARSHIP_WEATHER_PREVIOUS_BASH_PRECMD" >/dev/null; then
            "$_STARSHIP_WEATHER_PREVIOUS_BASH_PRECMD"
        fi
        _starship_weather_update
        return "$_sw_status"
    }
    starship_precmd_user_func=_starship_weather_bash_precmd
fi
