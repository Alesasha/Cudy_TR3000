param(
    [string]$BaseUrl = "http://127.0.0.1:18765",
    [string]$Username = "admin",
    [string]$Password = $env:CUDY_ADMIN_PASSWORD,
    [string]$PlaywrightCli = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($Password)) {
    throw "Set CUDY_ADMIN_PASSWORD or pass -Password. The password is not stored by this script."
}

if ([string]::IsNullOrWhiteSpace($PlaywrightCli)) {
    $candidate = Get-ChildItem "$env:LOCALAPPDATA\npm-cache\_npx" -Filter playwright-cli.cmd -Recurse -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $candidate) {
        throw "playwright-cli.cmd was not found. Install Playwright CLI first."
    }
    $PlaywrightCli = $candidate.FullName
}
$PlaywrightScript = [IO.Path]::GetFullPath((Join-Path (Split-Path $PlaywrightCli -Parent) "..\@playwright\cli\playwright-cli.js"))
if (-not (Test-Path -LiteralPath $PlaywrightScript)) {
    throw "Playwright CLI JavaScript entry point was not found: $PlaywrightScript"
}

$session = "cudy-admin-smoke-$PID"
$env:CUDY_ADMIN_SMOKE_URL = $BaseUrl.TrimEnd('/')
$env:CUDY_ADMIN_SMOKE_USER = $Username
$env:CUDY_ADMIN_SMOKE_PASSWORD = $Password

$code = @'
(page) => (async () => {
const base = __BASE_URL__;
const username = __ADMIN_USER__;
const password = __ADMIN_PASSWORD__;
const errors = [];
page.on('console', message => {
  if (message.type() === 'error') errors.push(message.text());
});
await page.goto(base + '/login?next=/admin');
await page.getByLabel('User').fill(username);
await page.getByLabel('Password').fill(password);
await page.getByRole('button', { name: 'Sign in' }).click();
await page.waitForURL(/\/admin/);
for (const name of ['Status', 'Users', 'Devices', 'Global Routes', 'Auto Cache']) {
  await page.getByRole('button', { name: new RegExp('^' + name) }).click();
  await page.waitForTimeout(100);
  const section = page.locator('section[data-admin-section]:not([hidden])');
  if (await section.count() !== 1) throw new Error(`Expected one visible section after ${name}`);
}
await page.setViewportSize({ width: 1280, height: 900 });
await page.getByRole('button', { name: /^Devices/ }).click();
const desktopOverflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
await page.setViewportSize({ width: 375, height: 812 });
await page.waitForTimeout(100);
const mobile = await page.evaluate(() => {
  const table = document.querySelector('#admin-agents table');
  const cell = table && table.querySelector('tbody td');
  return {
    pageOverflow: document.documentElement.scrollWidth - window.innerWidth,
    tableClass: table ? table.classList.contains('responsive-table') : false,
    cellDisplay: cell ? getComputedStyle(cell).display : '',
    cellLabel: cell ? cell.dataset.label || '' : ''
  };
});
if (desktopOverflow > 1) throw new Error(`Desktop page overflow: ${desktopOverflow}px`);
if (mobile.pageOverflow > 1) throw new Error(`Mobile page overflow: ${mobile.pageOverflow}px`);
if (!mobile.tableClass || mobile.cellDisplay !== 'flex' || !mobile.cellLabel) {
  throw new Error(`Responsive table failed: ${JSON.stringify(mobile)}`);
}
if (errors.length) throw new Error(`Console errors: ${errors.join('; ')}`);
console.log(JSON.stringify({ ok: true, desktopOverflow, mobile }));
return { ok: true, desktopOverflow, mobile };
})()
'@
function ConvertTo-JsSingleQuoted([string]$Value) {
    return "'" + $Value.Replace("\", "\\").Replace("'", "\'").Replace("`r", "\r").Replace("`n", "\n") + "'"
}
$code = $code.Replace("__BASE_URL__", (ConvertTo-JsSingleQuoted $env:CUDY_ADMIN_SMOKE_URL))
$code = $code.Replace("__ADMIN_USER__", (ConvertTo-JsSingleQuoted $env:CUDY_ADMIN_SMOKE_USER))
$code = $code.Replace("__ADMIN_PASSWORD__", (ConvertTo-JsSingleQuoted $env:CUDY_ADMIN_SMOKE_PASSWORD))

try {
    & node $PlaywrightScript "-s=$session" open "$($env:CUDY_ADMIN_SMOKE_URL)/login?next=/admin" | Out-Null
    $result = & node $PlaywrightScript "-s=$session" run-code $code 2>&1
    $rendered = $result -join [Environment]::NewLine
    if ($LASTEXITCODE -ne 0 -or $rendered -match '(?m)^### Error') {
        throw ($result -join [Environment]::NewLine)
    }
    $result | Write-Output
    Write-Host "Control admin rendered smoke passed."
}
finally {
    & node $PlaywrightScript "-s=$session" close 2>$null | Out-Null
    Remove-Item Env:CUDY_ADMIN_SMOKE_URL,Env:CUDY_ADMIN_SMOKE_USER,Env:CUDY_ADMIN_SMOKE_PASSWORD -ErrorAction SilentlyContinue
}
