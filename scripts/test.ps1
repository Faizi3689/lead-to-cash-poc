# Runs the full test suite against the database configured in .env
$env:TEST_DATABASE_URL = (Get-Content .env | Select-String '^DATABASE_URL=').Line.Substring(13)
pytest -q @args
