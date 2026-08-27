param([ValidateSet('setup','migrate','verify','seed','start','test','demo')] [string]$Command='start')
$ErrorActionPreference = 'Stop'
switch ($Command) {
  'setup'   { python -m venv .venv; .\.venv\Scripts\python -m pip install -e ".[dev]" }
  'migrate' { .\.venv\Scripts\rpt migrate }
  'verify'  { .\.venv\Scripts\rpt verify }
  'seed'    { .\.venv\Scripts\rpt seed }
  'start'   {
    docker compose run --rm api rpt migrate
    docker compose up --build -d
    $ready = $false
    for ($i=0; $i -lt 30; $i++) {
      try {
        $api = Invoke-RestMethod 'http://localhost:8000/ready'
        $mock = Invoke-RestMethod 'http://localhost:9000/health'
        if ($api.status -eq 'ready' -and $mock.status -eq 'ok') { $ready = $true; break }
      } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $ready) { throw 'Local services did not become healthy. Run docker compose logs.' }
    Write-Host 'RPT API:       http://localhost:8000/docs'
    Write-Host 'Provider mocks: http://localhost:9000/docs'
    docker compose logs -f
  }
  'test'    { .\.venv\Scripts\python -m pytest }
  'demo'    { .\.venv\Scripts\rpt demo }
}
