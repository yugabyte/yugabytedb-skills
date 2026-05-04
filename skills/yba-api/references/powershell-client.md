# PowerShell client for the YBA API

A standalone PowerShell wrapper for the YBA REST API. **No external module required** — paste the functions into a script, dot-source a `.ps1` file, or copy them into a profile. Works on PowerShell 7+ (Linux, macOS, Windows).

This mirrors the Python wrapper in [python-client.md](python-client.md) one-for-one, so the same recipes (in [recipes.md](recipes.md)) translate directly. If the user already has the [`powershell-yba`](https://github.com/dmrnft/powershell-yba) module installed, prefer its richer cmdlets — but this file is self-sufficient when they don't.

## The wrapper

```powershell
# Cache for customer-id-per-baseurl lookups
$Script:YbaCustomerIdCache = @{}

function Invoke-YbaRequest
{
    <#
        .SYNOPSIS
        Invokes a request against the YugabyteDB Anywhere REST API.

        .DESCRIPTION
        Standalone wrapper. Returns the API response, or — if the response
        contains a `taskUUID` and -Wait is supplied — polls the task until
        it terminates and returns the final task object.

        .PARAMETER BaseUrl
        Base URL of the YBA instance, e.g. https://yba.example.com.

        .PARAMETER Endpoint
        Path beginning with /api/v1/... or /api/v2/...

        .PARAMETER ApiToken
        SecureString API token. Use ConvertTo-SecureString -AsPlainText -Force
        on a stored token, or capture from Get-YbaApiToken.

        .PARAMETER CustomerId
        Customer (tenant) UUID. Looked up and cached if omitted.

        .PARAMETER Method
        Get | Post | Put | Patch | Delete. Defaults to Get.

        .PARAMETER Data
        Hashtable, PSCustomObject, or JSON string for POST/PUT/PATCH bodies.
        Hashtables and objects are JSON-encoded; strings are sent verbatim.

        .PARAMETER ContentType
        Defaults to application/json. Set to 'text/plain' for runtime-config PUTs.

        .PARAMETER Parameters
        Hashtable of query-string parameters.

        .PARAMETER SkipCertificateCheck
        Skip TLS verification (common with self-signed YBA installs).

        .PARAMETER Wait
        If the response carries a taskUUID, poll until percent==100 or
        status in ('Aborted','Failure'). Returns the final task.

        .PARAMETER WaitTimeout
        Overall task timeout in seconds. Defaults to 600.

        .PARAMETER PollInterval
        Seconds between task polls. Defaults to 2.

        .PARAMETER Timeout
        Per-HTTP-request timeout in seconds. Defaults to 60.
    #>
    [CmdletBinding(SupportsShouldProcess = $True)]
    param(
        [Parameter(Mandatory)] [System.Uri]$BaseUrl,
        [Parameter(Mandatory)] [String]$Endpoint,
        [Parameter(Mandatory)] [SecureString]$ApiToken,
        [String]$CustomerId,
        [ValidateSet('Get','Post','Put','Patch','Delete')] [String]$Method = 'Get',
        [Object]$Data,
        [String]$ContentType = 'application/json',
        [Hashtable]$Parameters,
        [Switch]$SkipCertificateCheck,
        [Switch]$Wait,
        [Int]$WaitTimeout = 600,
        [Int]$PollInterval = 2,
        [Int]$Timeout = 60
    )

    $tokenPlain = $ApiToken | ConvertFrom-SecureString -AsPlainText
    $headers = @{
        'Accept'              = 'application/json'
        'Content-Type'        = $ContentType
        'X-AUTH-YW-API-TOKEN' = $tokenPlain
    }

    $url = "$($BaseUrl.AbsoluteUri.TrimEnd('/'))/$($Endpoint.TrimStart('/'))"
    if ($Parameters)
    {
        $qs = ($Parameters.GetEnumerator() | ForEach-Object {
            "{0}={1}" -f [uri]::EscapeDataString($_.Key), [uri]::EscapeDataString([string]$_.Value)
        }) -join '&'
        $url = "${url}?${qs}"
    }

    $req = @{
        Uri = $url; Method = $Method; Headers = $headers
        TimeoutSec = $Timeout; SkipCertificateCheck = $SkipCertificateCheck
    }

    if ($Data -and $Method -in @('Post','Put','Patch'))
    {
        $req.Body = if ($Data -is [string]) { $Data }
                    elseif ($ContentType -eq 'application/json') { $Data | ConvertTo-Json -Depth 32 -Compress }
                    else { [string]$Data }
    }

    if (-not $PSCmdlet.ShouldProcess($url, "$Method $url"))
    {
        # WhatIf: show the request and (for read-only calls) still execute
        if ($Method -ne 'Get') { return }
    }

    try   { $response = Invoke-RestMethod @req }
    catch { throw "YBA $Method $url failed: $($_.ErrorDetails.Message ?? $_.Exception.Message)" }

    if (-not $Wait -or -not $response -or
        -not $response.PSObject.Properties['taskUUID']) { return $response }

    if (-not $CustomerId) { $CustomerId = Get-YbaCustomerId -BaseUrl $BaseUrl -ApiToken $ApiToken -SkipCertificateCheck:$SkipCertificateCheck }

    $taskUrl = "$($BaseUrl.AbsoluteUri.TrimEnd('/'))/api/v1/customers/$CustomerId/tasks/$($response.taskUUID)"
    $deadline = (Get-Date).AddSeconds($WaitTimeout)
    $task = $response
    while ((Get-Date) -lt $deadline)
    {
        $task = Invoke-RestMethod -Uri $taskUrl -Method Get -Headers $headers `
                  -TimeoutSec 10 -SkipCertificateCheck:$SkipCertificateCheck
        if ($task.percent -eq 100 -or $task.status -in @('Aborted','Failure')) { return $task }
        Start-Sleep -Seconds $PollInterval
    }
    return $task   # final state on timeout
}

function Get-YbaApiToken
{
    <#
        .SYNOPSIS
        POST /api/v1/api_login. Returns a SecureString token.
        Note: this rotates any previously issued token for the user.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [System.Uri]$BaseUrl,
        [Parameter(Mandatory)] [PSCredential]$Credential,
        [Switch]$SkipCertificateCheck,
        [Switch]$ReturnDetail
    )
    $body = @{ email = $Credential.UserName
               password = $Credential.GetNetworkCredential().Password } | ConvertTo-Json -Compress
    $r = Invoke-RestMethod -Uri "$($BaseUrl.AbsoluteUri.TrimEnd('/'))/api/v1/api_login" `
            -Method Post -Body $body `
            -Headers @{'Accept'='application/json';'Content-Type'='application/json'} `
            -SkipCertificateCheck:$SkipCertificateCheck
    if ($ReturnDetail) { return $r | Select-Object apiToken,apiTokenVersion,customerUUID,userUUID }
    return $r.apiToken | ConvertTo-SecureString -AsPlainText -Force
}

function Register-YbaInstance
{
    <#
        .SYNOPSIS
        POST /api/v1/register?generateApiToken=1. First-time setup of a fresh YBA.
        Falls back to Get-YbaApiToken if the instance is already registered.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [System.Uri]$BaseUrl,
        [Parameter(Mandatory)] [String]$Code,
        [Parameter(Mandatory)] [String]$Email,
        [Parameter(Mandatory)] [String]$Name,
        [Parameter(Mandatory)] [SecureString]$Password,
        [Switch]$SkipCertificateCheck
    )
    $body = @{ code=$Code; name=$Name; email=$Email
               password = ($Password | ConvertFrom-SecureString -AsPlainText) } | ConvertTo-Json -Compress
    try
    {
        return Invoke-RestMethod `
            -Uri "$($BaseUrl.AbsoluteUri.TrimEnd('/'))/api/v1/register?generateApiToken=1" `
            -Method Post -Body $body `
            -Headers @{'Accept'='application/json';'Content-Type'='application/json'} `
            -SkipCertificateCheck:$SkipCertificateCheck
    }
    catch
    {
        if ($_.ErrorDetails.Message -match 'cannot register multiple accounts')
        {
            $cred = [pscredential]::new($Email, $Password)
            return Get-YbaApiToken -BaseUrl $BaseUrl -Credential $cred `
                       -SkipCertificateCheck:$SkipCertificateCheck -ReturnDetail
        }
        throw
    }
}

function Get-YbaCustomerId
{
    <#
        .SYNOPSIS
        GET /api/v1/customers — returns the (cached) customer UUID for a single-tenant YBA.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [System.Uri]$BaseUrl,
        [Parameter(Mandatory)] [SecureString]$ApiToken,
        [Switch]$SkipCertificateCheck,
        [Switch]$Force
    )
    $key = $BaseUrl.AbsoluteUri
    if (-not $Force -and $Script:YbaCustomerIdCache.ContainsKey($key))
        { return $Script:YbaCustomerIdCache[$key] }

    $tokenPlain = $ApiToken | ConvertFrom-SecureString -AsPlainText
    $r = Invoke-RestMethod -Uri "$($key.TrimEnd('/'))/api/v1/customers" -Method Get `
            -Headers @{'Accept'='application/json'; 'X-AUTH-YW-API-TOKEN'=$tokenPlain} `
            -SkipCertificateCheck:$SkipCertificateCheck
    $id = $r[0].uuid
    $Script:YbaCustomerIdCache[$key] = $id
    return $id
}
```

## Idiomatic usage

Splat the connection args once; every subsequent call is one line.

```powershell
# 1. Get a token (or reuse a stored one)
$cred = Get-Credential 'admin@example.com'
$token = Get-YbaApiToken -BaseUrl 'https://yba.example.com' -Credential $cred -SkipCertificateCheck

# 2. Splat the connection args
$Yba = @{
    BaseUrl              = 'https://yba.example.com'
    ApiToken             = $token
    SkipCertificateCheck = $true
}
$Yba.CustomerId = Get-YbaCustomerId @Yba

# 3. Read
$universes = Invoke-YbaRequest @Yba -Endpoint "/api/v1/customers/$($Yba.CustomerId)/universes"

# 4. Write + wait for the task
$task = Invoke-YbaRequest @Yba -Method Post -Wait `
    -Endpoint "/api/v1/customers/$($Yba.CustomerId)/tables/backup" `
    -Data @{
        universeUUID      = $universes[0].universeUUID
        keyspace          = 'yugabyte'
        backupType        = 'PGSQL_TABLE_TYPE'
        storageConfigUUID = '<storage-uuid>'
    }
```

## Runtime config (text/plain bodies)

```powershell
$global = '00000000-0000-0000-0000-000000000000'

# Read
Invoke-YbaRequest @Yba -Endpoint `
    "/api/v1/customers/$($Yba.CustomerId)/runtime_config/$global/key/yb.node_agent.preflight_checks.max_python_version"

# Write — note ContentType=text/plain and Data is the raw value as a string
Invoke-YbaRequest @Yba -Method Put -ContentType 'text/plain' -Data '3.11' -Endpoint `
    "/api/v1/customers/$($Yba.CustomerId)/runtime_config/$global/key/yb.node_agent.preflight_checks.max_python_version"

# Per-universe boolean
Invoke-YbaRequest @Yba -Method Put -ContentType 'text/plain' -Data 'true' -Endpoint `
    "/api/v1/customers/$($Yba.CustomerId)/runtime_config/$($universes[0].universeUUID)/key/yb.checks.nodes_safe_to_take_down.enabled"
```

## Telemetry provider (S3 log export)

```powershell
$body = @{
    name   = 'logs-to-s3'
    config = @{
        type      = 'S3'
        region    = 'eu-west-1'
        bucket    = 'yba-log-archive'
        accessKey = 'AKIA...'
        secretKey = '...'
    }
    tags = @{ env = 'prod' }
}
Invoke-YbaRequest @Yba -Method Post -Data $body `
    -Endpoint "/api/v1/customers/$($Yba.CustomerId)/telemetry_provider"
```

## Universe health check + alerts

```powershell
$uni = ($universes | Where-Object name -EQ 'my-universe').universeUUID

# Trigger an on-demand check
Invoke-YbaRequest @Yba -Endpoint "/api/v1/customers/$($Yba.CustomerId)/universes/$uni/trigger_health_check"
Start-Sleep 30

# Latest health-check report for the universe
$checks = Invoke-YbaRequest @Yba -Endpoint "/api/v1/customers/$($Yba.CustomerId)/universes/$uni/health_check"
$latest = $checks | Select-Object -Last 1

# Active alerts scoped to this universe
$active = Invoke-YbaRequest @Yba -Endpoint "/api/v1/customers/$($Yba.CustomerId)/alerts/active"
$mine = $active | Where-Object { $_.sourceUUID -eq $uni -or $_.labels.source_uuid -eq $uni }
```

## What this wrapper deliberately does NOT do

- **No retries** — add them at the call site.
- **No multi-tenant lookup** — assumes a single-customer YBA (the supported topology today).
- **No automatic JSON shaping for v1 universe payloads** — for those, GET an existing universe, mutate, POST back, or use the v2 endpoints with their slimmer schema.
- **No `WhatIf` for state-changing endpoints** — the wrapper supports `-WhatIf` but for mutating verbs it will *skip* the request rather than dry-run it. To preview, set `$VerbosePreference='Continue'` and add `Write-Verbose` calls, or build the URL/body and write them out before invoking.
