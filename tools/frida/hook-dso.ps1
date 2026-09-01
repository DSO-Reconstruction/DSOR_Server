<#
.SYNOPSIS
    Hooks the Drakensang client with Frida and writes the capture into the share.

.DESCRIPTION
    Runs tools/frida/hookcap.py from \\host.lan\Data (the Linux home, mounted by
    WinBoat) against the running client. Output goes back into the share, so it
    can be analysed on the Linux side straight away with:

        python tools/frida/hookcap.py verify ~/dso-capture/session.jsonl

    Two modes:

      -SocketOnly   just the UDP payloads. Needs no addresses, so run this first
                    to prove Frida attaches and the socket hooks fire at all.
      (default)     also hooks RakNetStream::Write, the game's serialisation
                    entry point. The sequence of its calls for one message is
                    what defines that message's fields, which is the whole
                    reason for hooking rather than sniffing.

.EXAMPLE
    # Preuve que Frida s'attache et que les hooks socket se declenchent.
    powershell -ExecutionPolicy Bypass -File \\host.lan\Data\dsor-server\tools\frida\hook-dso.ps1 -SocketOnly

.EXAMPLE
    # Lire les arguments que le lanceur passe au client (jeu en cours d execution).
    powershell -ExecutionPolicy Bypass -File \\host.lan\Data\dsor-server\tools\frida\hook-dso.ps1 -ShowArgs

.EXAMPLE
    # Rejouer la ligne de commande du lanceur, en pointant le client sur ton serveur.
    powershell -ExecutionPolicy Bypass -File \\host.lan\Data\dsor-server\tools\frida\hook-dso.ps1 -Spawn `
        -CmdFile \\host.lan\Data\dso\cmd.txt -LoginIp 192.168.1.50:2190

.EXAMPLE
    # Capture complete depuis le lancement, sequence de login incluse.
    powershell -ExecutionPolicy Bypass -File \\host.lan\Data\dsor-server\tools\frida\hook-dso.ps1 -Spawn -SpawnArgs '--token','abc'

.EXAMPLE
    # Le jeu tourne deja: on ne verra que le gameplay en regime etabli.
    powershell -ExecutionPolicy Bypass -File \\host.lan\Data\dsor-server\tools\frida\hook-dso.ps1
#>
[CmdletBinding()]
param(
    # Process to attach to. The 64-bit client; pass -Target dro_client.exe for the 32-bit one.
    [string]$Target = 'dro_client64.exe',

    # Where hookcap.py lives, as seen from Windows.
    [string]$Share = '\\host.lan\Data',

    # Output directory, inside the share so Linux can read it.
    [string]$OutDir = '\\host.lan\Data\dso-capture',

    # Function to hook, module-relative, found in this build of dro_client64.exe.
    # 0xCCA214 = RakNetStream::Write(const void*, int)  -- the auth/string path
    # 0xCCA020 = RakNetStream::ReadBits(uchar*, uint)   -- the read path, needs :ret
    [string]$Offset = '0xCCA214',

    # Name recorded with each call, and the extra spec flags. Use -HookFlags
    # 'dumplen=48:ret' for a function that fills a buffer: its contents only
    # exist once the call returns.
    [string]$HookName = 'StreamWrite',
    [string]$HookFlags = 'dumplen=32',

    # Skip the offset hook and capture UDP payloads only.
    [switch]$SocketOnly,

    # Watch what the client does with a status effect, instead of the stream hook.
    #
    # Three points on the path that decides, read out of this build of
    # dro_client64.exe and given module-relative because ASLR moves the base:
    #
    #   0x34cca5  ClientStatusEffectManager's StatusEffectCommand handler
    #   0x34ee58  StatusEffectManager::StatusEffectTableRowToId(int) -- args[1] is
    #             the row index, so this says which effect the client understood
    #   0x98404c  the effect instance creation -- a null return means the element
    #             is dropped, and the client logs nothing at all when it is
    #
    # Cast one skill and the four possible outcomes name four different causes.
    [switch]$Effets,

    # Launch the client under Frida instead of attaching to a running one.
    # This is what makes the login sequence visible: the handshake, the 0x8A
    # credential, the 0x84 server handoff and the map load all happen in the
    # first seconds and never happen again, so attaching to a client that is
    # already in-game cannot see any of them.
    [switch]$Spawn,

    # Executable to launch in -Spawn mode. If the game is started by a launcher
    # that then runs the client, point this at the launcher and keep
    # -FollowChildren on, otherwise the interesting process escapes.
    [string]$SpawnPath = "$env:LOCALAPPDATA\Temp\DSOClient\dlcache\dro_client64.exe",

    # Instrument processes the spawned one launches. On by default in -Spawn mode.
    [bool]$FollowChildren = $true,

    # Arguments for the spawned executable. Pass them as separate array elements,
    # not one string, so nothing depends on how spaces are split:
    #   -SpawnArgs '--foo','bar baz'
    [string[]]$SpawnArgs = @(),

    # Working directory for the spawned executable. Defaults to the parent of the
    # exe's own folder, because the client lives in dlcache\ while its data
    # (export_win32_db_static.db4.unc, the bundles) sits one level up: started
    # from the wrong directory it cannot resolve its assigns and exits before
    # opening a socket, which is indistinguishable from a broken hook.
    [string]$WorkDir = '',

    # Read the launch arguments from a file holding the client's command line, as
    # produced by -ShowArgs or copied from the launcher. Saves retyping twenty
    # arguments, and avoids the transcription errors that come with it.
    [string]$CmdFile = '',

    # Replace the -ip value with this one. This is the single argument that
    # decides which login server the client dials, so it is what points a client
    # at your own server -- no patching, no DNS games.
    [string]$LoginIp = '',

    # Arguments dropped when reading -CmdFile. The window handles are HWNDs the
    # launcher owned and are meaningless in a new process; fullscreen just makes
    # debugging harder.
    [string[]]$DropArgs = @('-parentwindow', '-receiverwindow', '-fullscreen'),

    # Print the command line of the running client and exit. This is how you find
    # out which arguments the launcher passes, rather than guessing them.
    [switch]$ShowArgs,

    # Record a call stack whenever an outgoing payload contains this byte pattern.
    # 8b5f00 is the client's bulk gameplay message (0x8B, opcode 0x005F).
    [string]$Pattern = ''
)

$ErrorActionPreference = 'Stop'

# ── Logging ─────────────────────────────────────────────────────────────────
# Everything this script and the Python tool print is written to a file inside
# the share, so a failure can be read on the Linux side rather than retyped from
# a screenshot. Set up before anything else, because the errors most worth
# capturing come from the checks below.

$script:LogDir = if ($OutDir) { $OutDir } else { '\\host.lan\Data\dso-capture' }
$script:Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
if (-not (Test-Path $script:LogDir)) {
    New-Item -ItemType Directory -Force -Path $script:LogDir | Out-Null
}
$script:Transcript = Join-Path $script:LogDir ('run-' + $script:Stamp + '.log')
$script:PythonLog = Join-Path $script:LogDir ('python-' + $script:Stamp + '.log')
$script:TranscriptOn = $false
try {
    Start-Transcript -Path $script:Transcript -Force | Out-Null
    $script:TranscriptOn = $true
} catch {
    Write-Host "AVERTISSEMENT: transcription impossible: $($_.Exception.Message)" -ForegroundColor Yellow
}

function Stop-Logging {
    if ($script:TranscriptOn) {
        try { Stop-Transcript | Out-Null } catch { }
        $script:TranscriptOn = $false
    }
}

function Fail($message) {
    Write-Host "ERREUR: $message" -ForegroundColor Red
    Write-Host ""
    Write-Host "journal: $($script:Transcript)"
    Stop-Logging
    exit 1
}

# Catches any terminating error anywhere below, so a crash is recorded with its
# stack instead of vanishing when the window closes.
trap {
    Write-Host ""
    Write-Host "ERREUR NON RATTRAPEE" -ForegroundColor Red
    Write-Host "  message : $($_.Exception.Message)"
    Write-Host "  type    : $($_.Exception.GetType().FullName)"
    Write-Host "  ligne   : $($_.InvocationInfo.ScriptLineNumber) -> $($_.InvocationInfo.Line)"
    Write-Host "  pile    :"
    Write-Host $_.ScriptStackTrace
    Write-Host ""
    Write-Host "journal: $($script:Transcript)"
    Stop-Logging
    exit 1
}

Write-Host "== environnement ==" -ForegroundColor Cyan
Write-Host "  powershell : $($PSVersionTable.PSVersion) $($PSVersionTable.PSEdition)"
Write-Host "  os         : $([System.Environment]::OSVersion.VersionString)"
Write-Host "  64 bits    : os=$([System.Environment]::Is64BitOperatingSystem) process=$([System.Environment]::Is64BitProcess)"
Write-Host "  journal    : $($script:Transcript)"
Write-Host ""

# ── Discovering the arguments the launcher uses ──────────────────────────────
# The client is normally started by a launcher that passes it a session token and
# paths. Reading them off the live process beats guessing: the command line is
# recorded by Windows and readable without any instrumentation.
if ($ShowArgs) {
    $name = [IO.Path]::GetFileName($SpawnPath)
    $found = Get-CimInstance Win32_Process -Filter "Name='$name'" -ErrorAction SilentlyContinue
    if (-not $found) {
        Write-Host "$name ne tourne pas. Lance le jeu normalement (par son lanceur)," -ForegroundColor Yellow
        Write-Host 'puis relance cette commande pendant qu il tourne.'
        exit 1
    }
    foreach ($proc in $found) {
        Write-Host "pid $($proc.ProcessId)" -ForegroundColor Cyan
        Write-Host "  ligne de commande : $($proc.CommandLine)"
        Write-Host "  executable        : $($proc.ExecutablePath)"
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.ParentProcessId)" -ErrorAction SilentlyContinue
        if ($parent) {
            Write-Host "  lance par         : $($parent.Name) (pid $($parent.ProcessId))"
            Write-Host "                      $($parent.CommandLine)"
        }
    }
    Write-Host ''
    Write-Host 'Reporte les arguments dans -SpawnArgs, un par element du tableau.'
    exit 0
}

$hookcap = Join-Path $Share 'dsor-server\tools\frida\hookcap.py'
$agent   = Join-Path $Share 'dsor-server\tools\frida\dso_agent.js'

# ── Preflight ───────────────────────────────────────────────────────────────
# Each check exists because its failure is otherwise reported as "the client
# does not respond", which is the most expensive kind of error to debug.

Write-Host '== verifications ==' -ForegroundColor Cyan

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $python) { Fail 'python introuvable. Installe Python 3, puis: pip install frida-tools' }
Write-Host "  python   : $($python.Source)"

& $python.Source -c "import frida, sys; print('  frida    : ' + frida.__version__)"
if ($LASTEXITCODE -ne 0) { Fail 'module frida absent. pip install frida-tools' }

$arch = & $python.Source -c "import struct; print(struct.calcsize('P') * 8)"
if ($arch.Trim() -ne '64') {
    Fail "python est en $($arch.Trim()) bits. Le client est un PE x86-64, il faut un python 64 bits."
}
Write-Host "  python   : $($arch.Trim()) bits, correct pour un client x64"

if (-not (Test-Path $hookcap)) { Fail "hookcap.py introuvable a $hookcap. Le partage est-il monte ?" }
if (-not (Test-Path $agent))   { Fail "dso_agent.js introuvable a $agent" }
Write-Host "  hookcap  : $hookcap"

if ($Spawn) {
    if (-not (Test-Path $SpawnPath)) {
        Fail "executable introuvable: $SpawnPath. Passe -SpawnPath avec le bon chemin."
    }
    if (-not $WorkDir) {
        # Parent of the exe's directory: dlcache\ holds the binary, its data sits
        # one level up.
        $WorkDir = Split-Path (Split-Path $SpawnPath -Parent) -Parent
    }
    Write-Host "  mode     : spawn (les etapes de connexion seront visibles)"
    Write-Host "  binaire  : $SpawnPath"
    Write-Host "  cwd      : $WorkDir"
    if ($SpawnArgs.Count -gt 0) {
        Write-Host "  args     : $($SpawnArgs -join ' | ')"
    } else {
        Write-Host "  args     : aucun. Si le client sort aussitot, utilise -ShowArgs" -ForegroundColor Yellow
        Write-Host "             pendant qu il tourne pour lire ceux du lanceur."
    }
    $running = Get-Process -Name ([IO.Path]::GetFileNameWithoutExtension($SpawnPath)) -ErrorAction SilentlyContinue
    if ($running) {
        Write-Host "  ATTENTION: une instance tourne deja (pid $($running.Id -join ', '))." -ForegroundColor Yellow
        Write-Host "             Ferme-la, sinon le jeu peut refuser de demarrer deux fois."
    }
} else {
    $process = Get-Process -Name ([IO.Path]::GetFileNameWithoutExtension($Target)) -ErrorAction SilentlyContinue
    if (-not $process) {
        Fail "$Target ne tourne pas. Utilise -Spawn pour que Frida lance le jeu lui-meme, ce qui est le seul moyen de voir la sequence de connexion."
    }
    Write-Host "  mode     : attach (la sequence de connexion est deja passee, elle sera manquee)"
    Write-Host "  cible    : $Target (pid $($process.Id -join ', '))"
}

# ── Reading a recorded command line ─────────────────────────────────────────

if ($CmdFile) {
    if (-not (Test-Path $CmdFile)) { Fail "fichier introuvable: $CmdFile" }
    $raw = (Get-Content $CmdFile -Raw).Trim()
    $raw = $raw -replace '^\[FULL\]\s*', ''

    # Split on spaces but keep quoted runs together, then unquote.
    $tokens = [regex]::Matches($raw, '"[^"]*"|\S+') | ForEach-Object { $_.Value.Trim('"') }
    if ($tokens.Count -lt 1) { Fail "aucun argument lisible dans $CmdFile" }

    $exeFromFile = $tokens[0] -replace '/', '\'
    $parsed = @()
    $index = 1
    while ($index -lt $tokens.Count) {
        $token = $tokens[$index]
        # A flag's value is the next token unless that token is itself a flag.
        $hasValue = ($index + 1 -lt $tokens.Count) -and ($tokens[$index + 1] -notmatch '^-[A-Za-z]')
        if ($DropArgs -contains $token) {
            Write-Host "  ignore   : $token" -ForegroundColor DarkGray
            $index += if ($hasValue) { 2 } else { 1 }
            continue
        }
        if ($token -eq '-ip' -and $LoginIp) {
            $parsed += @('-ip', $LoginIp)
            Write-Host "  -ip      : $LoginIp (remplace $($tokens[$index + 1]))" -ForegroundColor Cyan
            $index += 2
            continue
        }
        $parsed += $token
        if ($hasValue) { $parsed += $tokens[$index + 1]; $index += 2 } else { $index += 1 }
    }
    $SpawnArgs = $parsed
    if (-not (Test-Path $SpawnPath)) { $SpawnPath = $exeFromFile }
    Write-Host "  cmdfile  : $CmdFile ($($SpawnArgs.Count) arguments retenus)"
}

$session = Join-Path $script:LogDir ('session-' + $script:Stamp + '.jsonl')
$stacks = Join-Path $script:LogDir ('stacks-' + $script:Stamp + '.jsonl')
$calls = Join-Path $script:LogDir ('calls-' + $script:Stamp + '.jsonl')

# ── Build the command ───────────────────────────────────────────────────────

if ($Spawn) {
    $arguments = @($hookcap, 'capture', '--spawn', $SpawnPath, '-o', $session, '--stacks', $stacks, '--calls', $calls)
    if ($FollowChildren) { $arguments += '--follow-children' }
    if ($WorkDir) { $arguments += @('--cwd', $WorkDir) }
    # The =VALUE form, as one token: every game argument starts with a dash, and
    # "--spawn-arg -standalone" would be read by argparse as two options rather
    # than as an option and its value.
    foreach ($spawnArg in $SpawnArgs) { $arguments += "--spawn-arg=$spawnArg" }
} else {
    $arguments = @($hookcap, 'capture', '--target', $Target, '-o', $session, '--stacks', $stacks, '--calls', $calls)
}

if ($Effets) {
    # :ret on the two that matter for their return value -- the resolved id and
    # whether an instance was created at all.
    $specs = @(
        'dro_client64.exe+0x34cca5:handler:args=2',
        'dro_client64.exe+0x34ee58:rowToId:args=2:ret',
        'dro_client64.exe+0x98404c:createInstance:args=4:ret'
    )
    foreach ($spec in $specs) {
        $arguments += @('--hook', $spec)
        Write-Host "  hook     : $spec"
    }
} elseif (-not $SocketOnly) {
    # args=3 covers (this, buffer, length) for a __thiscall member: Frida's args[0]
    # is the implicit this pointer, so the payload is args[1]. dump=1 hexdumps it.
    $spec = "dro_client64.exe+${Offset}:${HookName}:args=3:dump=1:${HookFlags}"
    $arguments += @('--hook', $spec)
    Write-Host "  hook     : $spec"
}
if ($Pattern) {
    $arguments += @('--pattern', $Pattern)
    Write-Host "  motif    : $Pattern (une pile est enregistree a chaque envoi le contenant)"
}

Write-Host ''
Write-Host '== capture en cours, Ctrl-C pour arreter ==' -ForegroundColor Green
Write-Host "  datagrammes -> $session"
Write-Host "  piles       -> $stacks"
Write-Host "  appels      -> $calls"
Write-Host ''
if ($Spawn) {
    Write-Host 'Le jeu va demarrer. Connecte-toi et entre en jeu: la sequence de'
    Write-Host 'login est ce que ce mode existe pour capturer. Bouge ensuite le'
    Write-Host 'personnage pour obtenir aussi les messages de gameplay.'
} else {
    Write-Host 'Joue quelques secondes: deplace le personnage, ce sont les messages'
    Write-Host 'qui portent le gameplay et donc ceux qui nous interessent.'
}
Write-Host ''

# Native stderr arrives in the pipeline as error records, and with the Stop
# preference that would abort the script on the first thing Python writes to
# stderr. Tee keeps it on screen and in the log at once.
$previousPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    # -u keeps Python's own output unbuffered: piped into Tee-Object it is
    # block-buffered otherwise, and everything still in the buffer is lost when
    # the run is ended with Ctrl-C.
    & $python.Source -u @arguments 2>&1 | Tee-Object -FilePath $script:PythonLog
} finally {
    $ErrorActionPreference = $previousPreference
}
Write-Host ""
Write-Host "code de sortie python : $LASTEXITCODE"

# ── After ───────────────────────────────────────────────────────────────────

Write-Host ''
if (Test-Path $session) {
    $lines = (Get-Content $session | Measure-Object -Line).Lines
    Write-Host "$lines datagrammes captures." -ForegroundColor Green
    $linuxPath = '~/dso-capture/' + (Split-Path $session -Leaf)
    Write-Host ''
    Write-Host 'Cote Linux, pour verifier que le codec comprend tout:'
    Write-Host "  python ~/dsor-server/tools/frida/hookcap.py verify $linuxPath"
} else {
    Write-Host 'Aucun fichier ecrit: la capture ne sest pas lancee.' -ForegroundColor Yellow
}

Write-Host ''
Write-Host "journal complet : $($script:Transcript)"
Write-Host "sortie python   : $($script:PythonLog)"
Stop-Logging
