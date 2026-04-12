declare module "openclaw/plugin-sdk" {
    export interface OpenClawPluginApi {
        pluginConfig: any;
        logger: {
            info: (msg: string) => void;
            warn: (msg: string) => void;
            error: (msg: string, err?: any) => void;
        };
        registerTool: (toolDef: any, meta?: any) => void;
        on: (eventHook: string, callback: (...args: any[]) => any) => void;
        [key: string]: any;
    }
}
