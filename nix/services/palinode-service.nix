# NixOS module for the Palinode API server and file-watcher services.
#
# Mirrors deploy/systemd/palinode-api.service.template and
# deploy/systemd/palinode-watcher.service.template.
#
# Usage in your NixOS configuration:
#
#   {
#     inputs.palinode.url = "github:phasespace-labs/palinode";
#     outputs = { palinode, ... }: {
#       nixosConfigurations.your-host = nixpkgs.lib.nixosSystem {
#         modules = [
#           palinode.nixosModules.palinode
#           ({ ... }: {
#             services.palinode.enable = true;
#             services.palinode.dataDir = "/var/lib/palinode";
#           })
#         ];
#       };
#     };
#   }

{ config, lib, pkgs, ... }:

let
  cfg = config.services.palinode;
in
{
  options.services.palinode = {
    enable = lib.mkEnableOption "Palinode API server and file-watcher services";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.palinode or (
        # Fall back to buildPythonApplication from the flake source when palinode
        # is not yet in nixpkgs. Community contributors: package this in nixpkgs
        # and remove this fallback.
        builtins.throw "palinode package not found in nixpkgs. Pass the package explicitly via services.palinode.package."
      );
      defaultText = lib.literalExpression "pkgs.palinode";
      description = "The palinode package to use.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "palinode";
      description = "System user under which palinode services run.";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "palinode";
      description = "System group under which palinode services run.";
    };

    dataDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/palinode";
      description = "Directory for palinode memory markdown files (PALINODE_DIR).";
    };

    apiHost = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = ''
        Host address for the palinode API server.
        Set to "0.0.0.0" for network-accessible deployments (e.g. Tailscale).
        When set to anything other than "127.0.0.1", a startup warning is emitted
        unless bindIntent is set to "public".
      '';
    };

    apiPort = lib.mkOption {
      type = lib.types.port;
      default = 6340;
      description = "Port for the palinode API server.";
    };

    ollamaUrl = lib.mkOption {
      type = lib.types.str;
      default = "http://localhost:11434";
      description = "Base URL for the Ollama API used for embeddings.";
    };

    embeddingModel = lib.mkOption {
      type = lib.types.str;
      default = "bge-m3";
      description = "Ollama model name used for embedding generation.";
    };

    bindIntent = lib.mkOption {
      type = lib.types.nullOr (lib.types.enum [ "public" ]);
      default = null;
      description = ''
        Set to "public" to suppress the 0.0.0.0-binding startup warning for
        intentional network-exposed deployments (e.g. behind Tailscale).
        Leave null to keep the warning when apiHost is "0.0.0.0".
      '';
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Whether to open apiPort in the NixOS firewall.";
    };
  };

  config = lib.mkIf cfg.enable {
    # Create the system user and group
    users.users.${cfg.user} = lib.mkDefault {
      isSystemUser = true;
      group = cfg.group;
      home = cfg.dataDir;
      createHome = false;
      description = "Palinode service user";
    };

    users.groups.${cfg.group} = lib.mkDefault { };

    # Ensure the data directory exists with correct ownership
    systemd.tmpfiles.rules = [
      "d '${cfg.dataDir}' 0750 ${cfg.user} ${cfg.group} - -"
    ];

    # Palinode API service (mirrors palinode-api.service.template)
    systemd.services.palinode-api = {
      description = "Palinode API Server (FastAPI / uvicorn)";
      documentation = [ "https://github.com/phasespace-labs/palinode" ];
      after = [ "network.target" ];
      wantedBy = [ "multi-user.target" ];

      environment = {
        PALINODE_DIR = cfg.dataDir;
        OLLAMA_URL = cfg.ollamaUrl;
        EMBEDDING_MODEL = cfg.embeddingModel;
      } // lib.optionalAttrs (cfg.bindIntent != null) {
        PALINODE_API_BIND_INTENT = cfg.bindIntent;
      };

      serviceConfig = {
        Type = "simple";
        User = cfg.user;
        Group = cfg.group;
        WorkingDirectory = cfg.dataDir;
        ExecStart = "${cfg.package}/bin/uvicorn palinode.api.server:app --host ${cfg.apiHost} --port ${toString cfg.apiPort}";
        Restart = "always";
        RestartSec = "5s";
        StandardOutput = "journal";
        StandardError = "journal";
        SyslogIdentifier = "palinode-api";

        # Security hardening
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ cfg.dataDir ];
        PrivateTmp = true;
      };
    };

    # Palinode watcher service (mirrors palinode-watcher.service.template)
    systemd.services.palinode-watcher = {
      description = "Palinode File Watcher & Indexer Daemon";
      documentation = [ "https://github.com/phasespace-labs/palinode" ];
      after = [ "network.target" "palinode-api.service" ];
      wants = [ "palinode-api.service" ];
      wantedBy = [ "multi-user.target" ];

      environment = {
        PALINODE_DIR = cfg.dataDir;
        OLLAMA_URL = cfg.ollamaUrl;
        EMBEDDING_MODEL = cfg.embeddingModel;
      };

      serviceConfig = {
        Type = "simple";
        User = cfg.user;
        Group = cfg.group;
        WorkingDirectory = cfg.dataDir;
        ExecStart = "${cfg.package}/bin/palinode-watcher";
        Restart = "always";
        RestartSec = "5s";
        StandardOutput = "journal";
        StandardError = "journal";
        SyslogIdentifier = "palinode-watcher";

        # Security hardening
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ cfg.dataDir ];
        PrivateTmp = true;
      };
    };

    # Optionally open the API port in the firewall
    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.apiPort ];
  };
}
