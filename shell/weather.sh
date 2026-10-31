# Source AFTER starship init; compatible with Bash and Zsh on Linux/macOS.

_starship_weather_clear() {
    unset STARSHIP_WEATHER
}

# Clear values left by the previous four-band implementation on re-source.
unset STARSHIP_WEATHER_BLUE STARSHIP_WEATHER_GREEN
unset STARSHIP_WEATHER_YELLOW STARSHIP_WEATHER_PINK

_starship_weather_accept() {
    local _sw_color _sw_code _sw_hex _sw_point
    [[ "$1" != *$'\n'* && "$1" != *$'\r'* ]] || return 1
    IFS=$'\t' read -r _sw_color _sw_code <<< "$1"
    [[ "$_sw_color" =~ ^#[0-9a-fA-F]{6}$ ]] || return 1
    [[ "$_sw_code" =~ ^[0-9a-fA-F]{4,6}$ ]] || return 1
    _sw_point=$((16#$_sw_code))
    (( (_sw_point >= 0xe000 && _sw_point <= 0xf8ff) ||
       (_sw_point >= 0xf0000 && _sw_point <= 0xffffd) ||
       (_sw_point >= 0x100000 && _sw_point <= 0x10fffd) )) || return 1
    _sw_hex=${_sw_color#\#}
    printf -v STARSHIP_WEATHER '\033[1;38;2;%d;%d;%dm%b\033[0m' \
        "$((16#${_sw_hex:0:2}))" "$((16#${_sw_hex:2:2}))" \
        "$((16#${_sw_hex:4:2}))" "\\U$_sw_code"
    export STARSHIP_WEATHER
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
    local _sw_root _sw_payload
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
    _starship_weather_accept "$_sw_payload" || _starship_weather_clear
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
