param([string]$BaseUrl = "http://127.0.0.1:4173")
$ErrorActionPreference = "Stop"
$paths = @(
    "/", "/styles.css", "/app.js", "/robots.txt", "/sitemap.xml", "/site.webmanifest",
    "/contacts.html", "/legal/privacy.html", "/legal/offer.html",
    "/legal/personal-data.html", "/legal/payment-refund.html", "/legal/terms.html"
)
foreach ($path in $paths) {
    $response = Invoke-WebRequest -UseBasicParsing "$BaseUrl$path"
    if ($response.StatusCode -ne 200) { throw "Smoke failed for $path" }
}
$homepage = (Invoke-WebRequest -UseBasicParsing "$BaseUrl/").Content
if ($homepage -notmatch "Pixora AI" -or $homepage -notmatch 'data-max-cta="hero"') {
    throw "Homepage content smoke failed"
}
if ($homepage -match 'href="https://max.ru/"' -or $homepage -notmatch '49 ₽') {
    throw "Homepage launch truth smoke failed"
}
Write-Output "Pixora site smoke passed ($BaseUrl)"
