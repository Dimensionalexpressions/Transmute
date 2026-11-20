{
  description = "Transmute - a converter that adapts to the machine it runs on";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f:
        nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      # Version is read from pyproject.toml so the two never drift apart.
      version = builtins.head (builtins.head (builtins.map
        (line: builtins.match "version = \"([0-9.]+)\"" line)
        (builtins.filter (nixpkgs.lib.hasPrefix "version = ")
          (nixpkgs.lib.splitString "\n" (builtins.readFile ./pyproject.toml)))));

      cleanSource = nixpkgs.lib.cleanSourceWith {
        src = nixpkgs.lib.cleanSource ./.;
        # .gui-live is a local scratch data dir, .venv is a local venv.
        filter = path: _type:
          let base = baseNameOf path;
          in !(base == ".venv" || base == ".gui-live" || base == ".pytest_cache"
               || nixpkgs.lib.hasSuffix ".zip" base || nixpkgs.lib.hasSuffix ".log" base);
      };

      mkTransmute = pkgs: { withEngines ? true }:
        let
          # Engines are EXTERNAL processes by design; the app probes them at
          # runtime and never assumes any hardware. Under Nix the native way
          # to provide an external process is PATH, so the full package wires
          # the reference engine builds into it. The probe still decides what
          # this machine can actually do - nothing is promised, everything
          # found is reported verbatim. transmute-minimal leaves PATH alone
          # and uses whatever engines the user provides (pip/venv style).
          engines = with pkgs; [ ffmpeg ghostscript qpdf libreoffice poppler-utils pandoc ];
          pythonEnv = pkgs.python3.withPackages (p: with p; [
            pillow pikepdf psutil platformdirs watchdog
            pyside6 fastapi uvicorn jinja2 python-multipart
            pip setuptools wheel build
          ]);
        in pkgs.python3.pkgs.buildPythonApplication {
          pname = "transmute";
          inherit version;
          format = "pyproject";
          src = cleanSource;

          python = pythonEnv;
          # The app is launched via `python -m ...`, not installed console
          # scripts, and its deps are the sealed pythonEnv above.
          dontWrapPythonPrograms = true;
          doCheck = false; # suite needs real engines + offscreen Qt; run via devShell / CI

          nativeBuildInputs = with pkgs; [
            makeWrapper qt6.wrapQtAppsHook
            # pypaBuildPhase runs `build --no-isolation`; the setuptools/wheel
            # backend must be importable by the build tool itself.
            python3Packages.pep517 python3Packages.setuptools python3Packages.wheel
          ];
          # wrapQtAppsHook needs a host Qt (sets qtPluginPrefix) to wrap the
          # GUI launcher against; qtsvg supplies the SVG image plugins.
          buildInputs = with pkgs; [ qt6.qtbase qt6.qtsvg ];
          propagatedBuildInputs = with pkgs.python3Packages; [
            pillow pikepdf psutil platformdirs watchdog
            pyside6 fastapi uvicorn jinja2 python-multipart
          ];

          postInstall = ''
            mkdir -p $out/bin
            makeWrapper ${pythonEnv}/bin/python $out/bin/transmute \
              --add-flags "-m transmute.gui.app" \
              ${pkgs.lib.optionalString withEngines
                ''--prefix PATH : "${pkgs.lib.makeBinPath engines}" ''}
            makeWrapper ${pythonEnv}/bin/python $out/bin/transmute-serve \
              --add-flags "-m transmute.serve" \
              ${pkgs.lib.optionalString withEngines
                ''--prefix PATH : "${pkgs.lib.makeBinPath engines}" ''}
          '';

          meta = with pkgs.lib; {
            description = "Media & document converter that adapts to the machine it runs on";
            homepage = "https://github.com/Dimensionalexpressions/transmute";
            license = {
              shortName = "FUAL-1.0";
              fullName = "Free Use Attribution License 1.0 (see LICENSE)";
              free = true;
              redistributable = true;
              deprecated = false;
            }; # same license as the duplicate-finder family
            platforms = platforms.linux;
            mainProgram = "transmute";
          };

          passthru.withEngines = withEngines;
        };
    in {
      packages = forAllSystems (pkgs: {
        transmute = mkTransmute pkgs { withEngines = true; };
        transmute-minimal = mkTransmute pkgs { withEngines = false; };
        default = mkTransmute pkgs { withEngines = true; };
      });

      apps = forAllSystems (pkgs: {
        transmute = { type = "app"; program = "${self.packages.${pkgs.system}.transmute}/bin/transmute"; };
        default = self.apps.${pkgs.system}.transmute;
      });

      devShells = forAllSystems (pkgs:
        let pythonEnv = pkgs.python3.withPackages (p: with p; [
              pillow pikepdf psutil platformdirs watchdog
              pyside6 fastapi uvicorn jinja2 python-multipart pytest
            ]);
        in {
          default = pkgs.mkShell {
            packages = with pkgs; [
              pythonEnv
              ffmpeg ghostscript qpdf libreoffice poppler-utils pandoc
              pkgs.qt6.qtbase pkgs.qt6.qtsvg # Qt runtime + image plugins for dev runs
              xorg.libxcb libxkbcommon
            ];
            shellHook = ''
              export PYTHONPATH=$PWD/src
              export QT_QPA_PLATFORM="''${QT_QPA_PLATFORM:-offscreen}"
              export TRANSMUTE_DISABLE_GPU=1
              # Nix-built Qt keeps plugins under lib/qt6/plugins (multi-out).
              export QT_PLUGIN_PATH="$(ls -d ${pkgs.qt6.qtbase}/lib*/qt6/plugins 2>/dev/null || true)''${QT_PLUGIN_PATH:+:$QT_PLUGIN_PATH}"
            '';
          };
        });
    };
}
