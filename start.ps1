param([int]$Port = 8000)
Set-Location -LiteralPath $PSScriptRoot
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -X utf8 app.py --port $Port --open
} else {
    & python -X utf8 app.py --port $Port --open
}
