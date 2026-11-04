param([Parameter(Mandatory=$true)][string]$Sdk,[Parameter(Mandatory=$true)][string]$Output)
$ErrorActionPreference='Stop'
$sdkPath=(Resolve-Path -LiteralPath $Sdk).Path
$outputPath=[IO.Path]::GetFullPath($Output)
if ($outputPath -eq $sdkPath) { throw 'Output must be separate from SDK' }
New-Item -ItemType Directory -Path $outputPath -Force | Out-Null
$compiler=Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$source=Join-Path $PSScriptRoot '..\qq_archive_next\desktop'
Add-Type -AssemblyName System.Drawing
$iconPath=Join-Path $outputPath 'QQArchiveNext.ico'
$images=@()
foreach($size in @(16,24,32,48,64,128,256)) {
 $bitmap=New-Object Drawing.Bitmap($size,$size)
 $graphics=[Drawing.Graphics]::FromImage($bitmap)
 $graphics.SmoothingMode=[Drawing.Drawing2D.SmoothingMode]::AntiAlias
 $graphics.Clear([Drawing.Color]::FromArgb(40,97,205))
 $font=New-Object Drawing.Font('Segoe UI',([single]($size*0.67)),[Drawing.FontStyle]::Bold,[Drawing.GraphicsUnit]::Pixel)
 $format=New-Object Drawing.StringFormat;$format.Alignment='Center';$format.LineAlignment='Center'
 $graphics.DrawString('Q',$font,[Drawing.Brushes]::White,[Drawing.RectangleF]::new(0,0,$size,$size),$format)
 $buffer=New-Object IO.MemoryStream;$bitmap.Save($buffer,[Drawing.Imaging.ImageFormat]::Png)
 $images+=@{size=$size;data=$buffer.ToArray()}
 $buffer.Dispose();$format.Dispose();$font.Dispose();$graphics.Dispose();$bitmap.Dispose()
}
$stream=[IO.File]::Create($iconPath);$writer=New-Object IO.BinaryWriter($stream)
try {
 $writer.Write([uint16]0);$writer.Write([uint16]1);$writer.Write([uint16]$images.Count);$offset=6+16*$images.Count
 foreach($entry in $images){$dimension=if($entry.size -eq 256){0}else{$entry.size};$writer.Write([byte]$dimension);$writer.Write([byte]$dimension);$writer.Write([byte]0);$writer.Write([byte]0);$writer.Write([uint16]1);$writer.Write([uint16]32);$writer.Write([uint32]$entry.data.Length);$writer.Write([uint32]$offset);$offset+=$entry.data.Length}
 foreach($entry in $images){$writer.Write([byte[]]$entry.data)}
} finally {$writer.Dispose();$stream.Dispose()}
$sources=@(Get-ChildItem -LiteralPath $source -Filter '*.cs' | Sort-Object Name | ForEach-Object FullName)
& $compiler /nologo /target:winexe /platform:x64 /optimize+ ("/out:"+(Join-Path $outputPath 'QQArchiveNext.exe')) ("/win32manifest:"+(Join-Path $source 'QQArchiveNext.manifest')) ("/win32icon:"+$iconPath) /r:System.Windows.Forms.dll /r:System.Drawing.dll /r:System.Net.Http.dll /r:System.Web.Extensions.dll ("/r:"+(Join-Path $sdkPath 'Microsoft.Web.WebView2.Core.dll')) ("/r:"+(Join-Path $sdkPath 'Microsoft.Web.WebView2.WinForms.dll')) $sources
if ($LASTEXITCODE) { throw 'Desktop compilation failed' }
foreach($name in @('Microsoft.Web.WebView2.Core.dll','Microsoft.Web.WebView2.WinForms.dll','WebView2Loader.dll')) { Copy-Item -LiteralPath (Join-Path $sdkPath $name) -Destination $outputPath }
