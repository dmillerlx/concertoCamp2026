#Requires -Version 5.1
<#
.SYNOPSIS
  Packages and deploys Concerto Camp 2026 schedule viewer to AWS.
.DESCRIPTION
  1. Ensures deploy S3 bucket exists
  2. Installs Python dependencies and zips Lambda package
  3. Uploads zip to S3
  4. Deploys CloudFormation stack
  5. Refreshes Lambda code
  6. Injects API URL + data URL into website HTML
  7. Uploads website to S3
  8. Optionally seeds schedule.json by uploading the local PDF
#>

$ErrorActionPreference = 'Continue'

$Region          = 'us-west-2'
$StackName       = 'concertocamp'
$CfDistributionId = 'E3JH2RVP6B1W80'

$Root = Split-Path -Parent $PSScriptRoot

Write-Host ""
Write-Host "=== Concerto Camp Deployment ===" -ForegroundColor Cyan
Write-Host "Region : $Region"
Write-Host "Stack  : $StackName"
Write-Host ""

# ── Step 0: Get AWS account ID ──────────────────────────────────────────────

Write-Host "[0/8] Getting AWS account ID..." -ForegroundColor Yellow
$AccountId = aws sts get-caller-identity --query Account --output text --region $Region
if (-not $AccountId) { throw "Could not retrieve AWS account ID. Check your AWS CLI credentials." }
Write-Host "      Account: $AccountId" -ForegroundColor Gray

$DeployBucket = "concertocamp-deploy-$AccountId"

# ── Step 1: Ensure deploy bucket exists ────────────────────────────────────

Write-Host "[1/8] Ensuring deploy bucket '$DeployBucket'..." -ForegroundColor Yellow
$null = aws s3api head-bucket --bucket $DeployBucket --region $Region 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "      Creating bucket..." -ForegroundColor Gray
    aws s3api create-bucket --bucket $DeployBucket --region $Region `
        --create-bucket-configuration LocationConstraint=$Region 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to create deploy bucket." }
    Write-Host "      Bucket created." -ForegroundColor Gray
} else {
    Write-Host "      Bucket already exists." -ForegroundColor Gray
}

# ── Step 2: Build Lambda package ───────────────────────────────────────────

Write-Host "[2/8] Building Lambda package..." -ForegroundColor Yellow

$ApiSrc = Join-Path $Root "lambda\api_handler"
$ApiPkg = Join-Path $Root "lambda\api_handler\package"
$ApiZip = Join-Path $Root "concertocamp-api.zip"

if (Test-Path $ApiPkg) { Remove-Item $ApiPkg -Recurse -Force }
New-Item -ItemType Directory -Path $ApiPkg | Out-Null

# Install Linux-compatible wheels so binaries run on Lambda (Amazon Linux 2)
python -m pip install --no-user -r "$ApiSrc\requirements.txt" -t $ApiPkg `
    --platform manylinux2014_x86_64 `
    --implementation cp `
    --python-version 3.12 `
    --only-binary :all: `
    -q
if ($LASTEXITCODE -ne 0) { throw "pip install failed." }

Copy-Item "$ApiSrc\handler.py" -Destination $ApiPkg

if (Test-Path $ApiZip) { Remove-Item $ApiZip -Force }
Compress-Archive -Path "$ApiPkg\*" -DestinationPath $ApiZip -Force
Write-Host "      Created concertocamp-api.zip" -ForegroundColor Gray

# ── Step 3: Upload Lambda zip to S3 ───────────────────────────────────────

Write-Host "[3/8] Uploading Lambda package to S3..." -ForegroundColor Yellow
aws s3 cp $ApiZip "s3://$DeployBucket/concertocamp-api.zip" --region $Region | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Failed to upload Lambda zip." }
Write-Host "      Uploaded." -ForegroundColor Gray

# ── Step 4: Deploy CloudFormation stack ───────────────────────────────────

Write-Host "[4/8] Deploying CloudFormation stack '$StackName'..." -ForegroundColor Yellow
Write-Host "      (This may take 2-3 minutes on first deploy)" -ForegroundColor Gray

$templatePath = "$Root\template.yaml"
aws cloudformation deploy `
    --stack-name $StackName `
    --template-file $templatePath `
    --capabilities CAPABILITY_NAMED_IAM `
    --region $Region `
    --parameter-overrides `
        "DeployBucketName=$DeployBucket"

if ($LASTEXITCODE -ne 0) { throw "CloudFormation deployment failed." }
Write-Host "      Stack deployed successfully." -ForegroundColor Green

# Force Lambda code update (CF may skip if S3 key unchanged)
Write-Host "      Refreshing Lambda function code..." -ForegroundColor Gray
aws lambda update-function-code --function-name concertocamp-api-handler `
    --s3-bucket $DeployBucket --s3-key concertocamp-api.zip --region $Region | Out-Null

# ── Step 5: Get stack outputs ──────────────────────────────────────────────

Write-Host "[5/8] Reading stack outputs..." -ForegroundColor Yellow

$outputs = aws cloudformation describe-stacks `
    --stack-name $StackName `
    --region $Region `
    --query "Stacks[0].Outputs" `
    --output json | ConvertFrom-Json

$ApiUrl      = ($outputs | Where-Object { $_.OutputKey -eq 'ApiUrl' }).OutputValue
$WebsiteUrl  = ($outputs | Where-Object { $_.OutputKey -eq 'WebsiteUrl' }).OutputValue
$WebBucket   = ($outputs | Where-Object { $_.OutputKey -eq 'WebsiteBucketName' }).OutputValue
$DataBucket  = ($outputs | Where-Object { $_.OutputKey -eq 'DataBucketName' }).OutputValue

$DataUrl = "https://$DataBucket.s3.$Region.amazonaws.com"

Write-Host "      API URL    : $ApiUrl" -ForegroundColor Gray
Write-Host "      Website URL: $WebsiteUrl" -ForegroundColor Gray
Write-Host "      Data URL   : $DataUrl" -ForegroundColor Gray

# ── Step 6: Inject URLs and upload website ─────────────────────────────────

Write-Host "[6/8] Uploading website..." -ForegroundColor Yellow

$htmlSrc  = Join-Path $Root "website\index.html"
$htmlDest = Join-Path $Root "website\index.deployed.html"

$html = Get-Content $htmlSrc -Raw -Encoding UTF8
$html = $html -replace '__API_URL__',  $ApiUrl
$html = $html -replace '__DATA_URL__', $DataUrl
$html | Out-File $htmlDest -Encoding utf8 -NoNewline

aws s3 cp $htmlDest "s3://$WebBucket/index.html" `
    --content-type "text/html" `
    --region $Region | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Failed to upload website." }
Write-Host "      Website uploaded." -ForegroundColor Gray

# ── Step 7: Seed schedule.json (optional) ─────────────────────────────────

Write-Host "[7/8] Invalidating CloudFront cache..." -ForegroundColor Yellow
aws cloudfront create-invalidation --distribution-id $CfDistributionId --paths "/*" | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "      Invalidation queued." -ForegroundColor Gray
} else {
    Write-Host "      Invalidation failed (non-fatal)." -ForegroundColor Yellow
}

Write-Host "[8/8] Seeding schedule from PDF..." -ForegroundColor Yellow

$pdfPath = Join-Path (Split-Path -Parent $Root) "data\Student Schedule at TCC 2026.pdf"
if (Test-Path $pdfPath) {
    $pdfBytes  = [System.IO.File]::ReadAllBytes($pdfPath)
    $pdfBase64 = [System.Convert]::ToBase64String($pdfBytes)
    $body = @{ password = 'campschedule'; pdf = $pdfBase64 } | ConvertTo-Json -Compress

    $response = Invoke-RestMethod -Uri "$ApiUrl/upload" `
        -Method Post `
        -Body $body `
        -ContentType 'application/json' `
        -ErrorAction SilentlyContinue

    if ($response.success) {
        Write-Host "      Seeded: $($response.students) students, $($response.events) events." -ForegroundColor Green
    } else {
        Write-Host "      Seed failed or skipped. Upload via the site admin panel." -ForegroundColor Yellow
    }
} else {
    Write-Host "      PDF not found at $pdfPath - upload via the site admin panel." -ForegroundColor Yellow
}

# ── Done ──────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "==========================================" -ForegroundColor Green
Write-Host "  Concerto Camp deployment complete!" -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Site:  https://concertocamp2026.com" -ForegroundColor Cyan
Write-Host "  (S3):  $WebsiteUrl" -ForegroundColor Gray
Write-Host "  API:   $ApiUrl" -ForegroundColor Cyan
Write-Host ""
Write-Host "  To change the upload password:" -ForegroundColor Yellow
Write-Host "  aws ssm put-parameter --name '/concertocamp/upload_password' --value 'NEW_PASSWORD' --overwrite --region $Region" -ForegroundColor White
Write-Host ""
Write-Host "  To tear down:" -ForegroundColor Yellow
Write-Host "  aws cloudformation delete-stack --stack-name $StackName --region $Region" -ForegroundColor White
Write-Host ""
