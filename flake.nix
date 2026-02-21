{
  description = "CHUJ modpack development shell";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f:
        nixpkgs.lib.genAttrs systems (system: f {
          pkgs = import nixpkgs { inherit system; };
        });
    in
    {
      devShells = forAllSystems ({ pkgs }: {
        default = pkgs.mkShell {
          packages = with pkgs; [
            python312
            jq
            p7zip
          ];

          shellHook = ''
            echo "CHUJ dev shell ready: python3, jq, 7z"
          '';
        };
      });
    };
}
