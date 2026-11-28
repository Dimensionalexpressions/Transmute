TRANSMUTE for Windows - install in 4 steps
==========================================

1. Unzip Transmute-windows-x86_64.zip anywhere (e.g. C:\Transmute).
   No installer, no admin rights, nothing else to run. Start Transmute.exe.

2. Install the converter engines - they are NOT inside the zip (each has its
   own license). In PowerShell:
     winget install Gyan.FFmpeg
     winget install QPDF.QPDF
     winget install oschwartz10612.Poppler
     winget install JohnMacFarlane.Pandoc
     winget install TheDocumentFoundation.LibreOffice
   Ghostscript: download the official .msi from
   https://ghostscript.com/releases/gsdnld.html and install it.
   If a step fails, Transmute's button "Get and install a missing engine..."
   shows you the exact command or opens the official page.

3. Check what Transmute found: the engine status list in the app must show
   the engines you installed. Missing ones are named honestly and never faked.

4. Verify your download against SHA256SUMS.txt:
     certutil -hashfile Transmute-windows-x86_64.zip SHA256
   The printed value must match the line for the zip in SHA256SUMS.txt.

Notes
- The app asks SmartScreen "unknown publisher" - expected for unsigned alpha
  builds; choose "More info" -> "Run anyway".
- "Serve (convert from other devices)" lets phones/laptops send files to
  this PC over your LAN; the port shown in that panel is yours to open on
  your firewall if you use it.
