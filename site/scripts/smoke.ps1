param(
    [string]$BaseUrl = "http://127.0.0.1:4173",
    [string]$BrandName = "Ravuna"
)
$ErrorActionPreference = "Stop"
$paths = @(
    "/", "/styles.css", "/robots.txt", "/sitemap.xml", "/site.webmanifest",
    "/contacts.html", "/payment-success.html", "/payment-failed.html",
    "/legal/privacy.html", "/legal/offer.html",
    "/legal/personal-data.html", "/legal/payment-refund.html", "/legal/terms.html"
)
foreach ($path in $paths) {
    $response = Invoke-WebRequest -UseBasicParsing "$BaseUrl$path"
    if ($response.StatusCode -ne 200) { throw "Smoke failed for $path" }
}
$homepage = (Invoke-WebRequest -UseBasicParsing "$BaseUrl/").Content
if ($homepage -notmatch "$([regex]::Escape($BrandName)) AI" -or $homepage -notmatch 'data-max-cta="hero"') {
    throw "Homepage content smoke failed"
}
if ($homepage -match 'href="https://max.ru/"' -or $homepage -notmatch '(?<!\d)49(?!\d)') {
    throw "Homepage launch truth smoke failed"
}
$success = (Invoke-WebRequest -UseBasicParsing "$BaseUrl/payment-success.html").Content
$failed = (Invoke-WebRequest -UseBasicParsing "$BaseUrl/payment-failed.html").Content
if ($success -notmatch 'data-payment-return="success"' -or $success -notmatch "ResultURL") {
    throw "Payment success page content smoke failed"
}
if ($failed -notmatch 'data-payment-return="failed"' -or $failed -notmatch "viner-89@mail.ru") {
    throw "Payment failed page content smoke failed"
}
Write-Output "$BrandName site smoke passed ($BaseUrl)"
