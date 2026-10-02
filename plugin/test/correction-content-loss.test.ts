import { afterEach, expect, it, vi } from "vitest";
import palinodePlugin from "../index";

afterEach(() => vi.unstubAllGlobals());

it.each(["preview", "apply"])("forwards content-loss consent and reports omitted text for %s", async phase => {
    const tools: Record<string, any> = {};
    palinodePlugin.register({
        pluginConfig: { palinodeApiUrl: "http://fixture.test" },
        logger: { info: vi.fn(), warn: vi.fn(), error: vi.fn() },
        on: vi.fn(), registerCli: vi.fn(), registerService: vi.fn(),
        registerTool: (tool: any) => {
            if (typeof tool === "function") tool = tool({});
            tools[tool.name] = tool;
        },
    } as any);
    const omitted = "Failed notifications go to the quartz dead-letter queue.";
    const fetch = vi.fn(async (..._args: any[]) => Response.json({ content_loss: { removed_text: [omitted] } }));
    vi.stubGlobal("fetch", fetch);
    const tool = tools[`palinode_correction_${phase}`];
    expect(tool.parameters.properties.allow_content_loss.type).toBe("boolean");
    for (const consent of [undefined, false, true]) {
        const result = await tool.execute("test", {
            target: "decisions/notifier", replacement: "The transport is new.",
            confirm: true, expect_revision: "revision", allow_content_loss: consent,
        });
        const [url, options] = fetch.mock.lastCall as [string, RequestInit];
        expect(url).toContain(`/corrections/${phase}`);
        expect(JSON.parse(String(options.body)).allow_content_loss).toBe(consent);
        expect(result.content[0].text).toContain(omitted);
    }
});
