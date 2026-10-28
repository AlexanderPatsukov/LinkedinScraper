<#
.SYNOPSIS
    Собирает и запускает LinkedIn Jobs: админка Django + PostgreSQL.

.DESCRIPTION
    Один скрипт для чистой машины: ждёт движок Docker, создаёт .env, дотягивает
    базовые образы с повторами (прокси Docker Desktop после старта отвечает не
    сразу), собирает образ, поднимает оба контейнера и ждёт, пока админка
    ответит. Работает и в pwsh на Linux - сам Docker не запускает.

.EXAMPLE
    .\build.ps1
    .\build.ps1 -Rebuild      # пересборка без кеша
    .\build.ps1 -Reset        # снести базу и начать с нуля
    .\build.ps1 -Down         # остановить
    .\build.ps1 -Logs         # смотреть логи
#>
[CmdletBinding()]
param(
    [switch]$Rebuild,
    [switch]$Reset,
    [switch]$Down,
    [switch]$Logs
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

function Write-Step { param([string]$Text) Write-Host "`n==> $Text" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Text) Write-Host "    $Text" -ForegroundColor Green }
function Write-Warn { param([string]$Text) Write-Host "    $Text" -ForegroundColor Yellow }

# --- Docker: наличие, запуск, готовность движка ------------------------

function Wait-DockerEngine {
    param([int]$TimeoutSec = 180)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $version = docker version --format '{{.Server.Version}}' 2>$null
        # Сразу после старта Docker Desktop `docker info` уже отвечает, а версия
        # сервера ещё пустая - значит движок не готов принимать работу.
        if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($version)) {
            return $version.Trim()
        }
        Start-Sleep -Seconds 3
    }
    return $null
}

Write-Step 'Проверяю Docker'
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker не найден. Установите Docker: https://docs.docker.com/get-docker/'
}

$serverVersion = docker version --format '{{.Server.Version}}' 2>$null
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($serverVersion)) {
    Write-Warn 'Движок Docker не отвечает, жду до 4 минут (запустите Docker, если он не запущен)'
    $serverVersion = Wait-DockerEngine -TimeoutSec 240
    if (-not $serverVersion) {
        throw 'Движок Docker так и не поднялся. Запустите Docker и повторите.'
    }
}
Write-Ok "docker $serverVersion"

$compose = @('compose')
docker compose version 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    if (Get-Command docker-compose -ErrorAction SilentlyContinue) { $compose = $null }
    else { throw 'Не найден ни "docker compose", ни "docker-compose".' }
}

# Функция НИЧЕГО не возвращает: вывод docker идёт в пайплайн, и если добавить
# сюда `return $LASTEXITCODE`, вызывающий получит массив из строк вывода и кода.
# Код возврата читаем из $LASTEXITCODE сразу после вызова.
function Invoke-ComposeRaw {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$ComposeArgs)
    if ($null -eq $compose) { & docker-compose @ComposeArgs } else { & docker @compose @ComposeArgs }
}

function Invoke-Compose {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$ComposeArgs)
    Invoke-ComposeRaw @ComposeArgs
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose $($ComposeArgs -join ' ') завершился с кодом $LASTEXITCODE"
    }
}

# --- Сервисные режимы --------------------------------------------------
if ($Down) {
    Write-Step 'Останавливаю контейнеры'
    Invoke-Compose down
    Write-Ok 'Остановлено. База сохранена (том pgdata цел).'
    return
}
if ($Logs) {
    Invoke-ComposeRaw logs -f --tail 200
    return
}

# --- .env --------------------------------------------------------------
Write-Step 'Проверяю .env'
if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Write-Ok '.env создан из .env.example (логин admin / пароль admin)'
} else {
    Write-Ok '.env уже есть - не трогаю'
}

# --- Сброс -------------------------------------------------------------
if ($Reset) {
    Write-Step 'Сношу контейнеры и базу'
    Invoke-ComposeRaw down -v
    Write-Warn 'Том с данными удалён.'
}

# --- Базовые образы ----------------------------------------------------
# Тянем их отдельно и с повторами: сразу после старта Docker Desktop его
# внутренний прокси (http.docker.internal) какое-то время таймаутит запросы
# к Docker Hub, и сборка падает на шаге "load metadata".

function Get-BaseImages {
    $images = New-Object System.Collections.Generic.List[string]

    if (Test-Path 'Dockerfile') {
        foreach ($hit in Select-String -Path 'Dockerfile' -Pattern '(?im)^\s*FROM\s+([^\s]+)') {
            $images.Add($hit.Matches[0].Groups[1].Value)
        }
    }
    if (Test-Path 'docker-compose.yml') {
        foreach ($hit in Select-String -Path 'docker-compose.yml' -Pattern '(?im)^\s*image:\s*([^\s#]+)') {
            $image = $hit.Matches[0].Groups[1].Value
            # образ, который собираем сами, тянуть неоткуда
            if ($image -notlike '*linkedin-jobs*') { $images.Add($image) }
        }
    }
    if ($images.Count -eq 0) { $images.AddRange([string[]]@('python:3.12-slim', 'postgres:16-alpine')) }
    return $images | Select-Object -Unique
}

function Request-Image {
    param([string]$Image, [int]$Attempts = 6)

    docker image inspect $Image 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0 -and -not $Rebuild) {
        Write-Ok "$Image - уже есть"
        return $true
    }

    for ($i = 1; $i -le $Attempts; $i++) {
        docker pull $Image 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "$Image - скачан"
            return $true
        }
        if ($i -lt $Attempts) {
            $wait = [Math]::Min(10 * $i, 40)
            Write-Warn "$Image - попытка $i из $Attempts не удалась, жду $wait с (прокси Docker Desktop прогревается)"
            Start-Sleep -Seconds $wait
        }
    }
    return $false
}

Write-Step 'Проверяю базовые образы'
$missing = @()
foreach ($image in Get-BaseImages) {
    if (-not (Request-Image -Image $image)) { $missing += $image }
}
if ($missing.Count -gt 0) {
    throw ("Не удалось скачать: {0}. Проверьте интернет и что Docker Desktop полностью запустился, затем повторите." -f ($missing -join ', '))
}

# --- Сборка ------------------------------------------------------------
Write-Step 'Собираю образ'
$built = $false
for ($i = 1; $i -le 3; $i++) {
    # Без сплата: массив из одного элемента PowerShell схлопывает в строку,
    # и splat разбирает её посимвольно ("compose b").
    if ($Rebuild) { Invoke-ComposeRaw build --no-cache --pull } else { Invoke-ComposeRaw build }
    if ($LASTEXITCODE -eq 0) { $built = $true; break }
    if ($i -lt 3) {
        Write-Warn "Сборка не удалась (попытка $i из 3), жду 15 с и пробую ещё раз"
        Start-Sleep -Seconds 15
    }
}
if (-not $built) { throw 'Сборка образа не удалась. Смотрите вывод выше.' }

# --- Запуск ------------------------------------------------------------
Write-Step 'Поднимаю контейнеры (web + db)'
$up = $false
for ($i = 1; $i -le 3; $i++) {
    Invoke-ComposeRaw up -d
    if ($LASTEXITCODE -eq 0) { $up = $true; break }
    if ($i -lt 3) {
        Write-Warn "Запуск не удался (попытка $i из 3), жду 15 с и пробую ещё раз"
        Start-Sleep -Seconds 15
    }
}
if (-not $up) { throw 'Не удалось поднять контейнеры. Смотрите вывод выше.' }

# --- Ожидание админки --------------------------------------------------
function Get-EnvValue {
    param([string]$Name, [string]$Default = '')
    $line = Get-Content '.env' | Where-Object { $_ -match "^\s*$Name\s*=" } | Select-Object -Last 1
    if (-not $line) { return $Default }
    $value = ($line -split '=', 2)[1].Trim()
    if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
    return $value
}

$port = Get-EnvValue -Name 'WEB_PORT' -Default '8000'
$url = "http://localhost:$port"

Write-Step "Жду админку на $url"
$ready = $false
foreach ($attempt in 1..90) {
    try {
        if ((Invoke-WebRequest -Uri "$url/healthz" -TimeoutSec 3 -UseBasicParsing).StatusCode -eq 200) {
            $ready = $true
            break
        }
    } catch {
        Start-Sleep -Seconds 2
    }
}

if (-not $ready) {
    Write-Warn 'Админка не ответила за 3 минуты. Логи:'
    Invoke-ComposeRaw logs --tail 60 web
    throw 'Запуск не подтверждён. Разберитесь по логам выше.'
}

$user = Get-EnvValue -Name 'DJANGO_SUPERUSER_USERNAME' -Default 'admin'
$pass = Get-EnvValue -Name 'DJANGO_SUPERUSER_PASSWORD' -Default 'admin'

Write-Host ''
Write-Host '  Готово.' -ForegroundColor Green
Write-Host ''
Write-Host "  Админка:  $url/admin/"
Write-Host "  Логин:    $user"
Write-Host "  Пароль:   $pass"
Write-Host ''
Write-Host '  Логи:     .\build.ps1 -Logs'
Write-Host '  Стоп:     .\build.ps1 -Down'
Write-Host '  С нуля:   .\build.ps1 -Reset'
Write-Host ''
