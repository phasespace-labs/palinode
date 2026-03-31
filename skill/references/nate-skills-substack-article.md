Your Best AI Work Vanishes Every Session. 4 Prompts That Make It Permanent plus Access to My Skills Repo
Skills can be a powerful hidden context layer, but only if your agents can read them.
Nate
Mar 30, 2026
∙ Paid
On March 11th, Anthropic slipped Skills into the sidebars of Excel and PowerPoint. The internet shrugged.

Which is fair. Skills have always had a Toyota problem: the workhorse nobody glorifies but that runs the world. The Camry doesn’t trend. It just keeps going.

Most people still think of skills the way they worked in October — as a personal prompting shortcut. You encoded your methodology into a Markdown file, stopped re-explaining it on every task, saved yourself some setup time. That framing was right when you were the only one calling your skill, sitting at a keyboard, with enough judgment to catch the output when it drifted.

Four things changed in five months, and together they break that framing completely. Agents now invoke skills as often as humans do, with no one watching and no one to redirect. Team admins provision skills across entire organizations with a single upload. The format became a cross-industry standard adopted by OpenAI, Microsoft, GitHub, and Cursor, with 500,000 skills now running across platforms interchangeably. And the same file that lived in a developer terminal now runs in Excel, PowerPoint, and M365.

Almost everything built to the October standard is underspecified for the March reality. The conventional approach, building a skill that works when you’re watching, quietly fails the moment the human leaves the loop. And the human is leaving the loop faster than most people have registered.

Here’s what’s inside:

The architecture and what people are building on it. Progressive disclosure, the specialist stack, the orchestrator pattern, and why the same file now runs from a developer terminal to an Excel sidebar.

How to build a skill that works in March. The description field where most skills fail, the five things every skill body needs, and why building from your outputs beats building from your intentions.

The failure asymmetry that changes everything. Why “good enough for my use” and “good enough for agents” are categorically different standards, and the four redesigns that close the gap.

The team-level case and the ecosystem gap. Three tiers most organizations get backwards, and why 500,000 skills exist and almost none are for knowledge work.

Four prompts to build your first skill this week. The backlog audit, the output-extraction builder, the agent-readiness stress test, and the team deployment planner.

Let’s get into it.

Subscribers get all posts like these!

LINK: Grab the prompts
Most people who finish this article will agree they should build skills and then not do it, because the distance between “I should encode my methodology” and actually sitting down to write a SKILL.md from scratch is where good intentions stall. These four prompts close that distance. The first one runs the backlog audit: it interviews you about your recurring AI workflows and tells you which tasks are actually worth encoding and in what order. The second builds the skill for you using the output-extraction method: you paste in examples of your best work, it reverse-engineers the methodology you can’t articulate from intention alone, and delivers a copy-paste-ready SKILL.md with a routing-optimized description, specified output format, and edge case handling. The third takes that skill and stress-tests it against the agent-caller standard — the 10-to-15% vs. 100% failure asymmetry — and produces a hardened version. The fourth zooms out to the team level: which skills are organizational infrastructure, who should build them, and how to deploy so expertise stops walking out the door. All four work in Claude, ChatGPT, or Gemini.

The OB1 community skills directory has production-ready knowledge-work skills: competitive analysis, financial model review, deal memo drafting, research synthesis, and meeting synthesis, all built to the March standard, plus a contribution template if you want to share what you build.

What skills actually are
A skill is a folder. Inside it, one required file: SKILL.md. That file has two parts: YAML frontmatter at the top, instructions in Markdown below.

competitive-analysis/
├── SKILL.md            ← required; this is the whole thing
├── references/         ← optional; docs Claude loads when needed
├── scripts/            ← optional; executable code
└── assets/             ← optional; templates, examples
The frontmatter has two required fields:

---
name: competitive-analysis
description: What this skill does and when Claude should use it.
---
Everything below the closing --- is your instructions. That’s the complete structure. A working skill is literally a folder with a text file in it.

But there’s an architecture detail that changes how you design everything, and almost nobody explains it clearly.

Only the frontmatter is always in Claude’s context. The description field — 1,024 characters maximum — gets loaded into the system prompt at startup for every installed skill. The full SKILL.md body, your references, your scripts: none of that loads until Claude decides the skill is relevant. Anthropic calls this progressive disclosure: metadata first, full instructions only on match, supporting files only when specifically needed. A lean skill’s metadata is 50 to 100 tokens. Twenty installed skills cost you maybe 1,500 tokens of system prompt. The rest is zero until it fires.

The implication is architectural: the description field isn’t a label for humans to read in a settings panel. It’s the mechanism by which Claude routes to your skill. The only part of your skill that’s always present, always evaluated. Write it accordingly.

What people are building right now
The most common production pattern in Claude Code is what people are calling the specialist stack. A developer drops a folder of skills into their project — one for turning a vague feature request into a PRD, one for decomposing that into issues, one for writing tests before code, one for reviewing against the team’s architecture standards — and then types a single prompt. The agent reads description fields, routes through the stack, spawns subagents via context: fork for isolated execution, and delivers a pull request. No babysitting. The methodology is in the files.

The multifamily real estate GP @TXpaintbrush on X built the same pattern for operations with the same underlying logic. Every project gets its own folder with a Claude.md file containing the context and guardrails, detailed PRDs and architecture decision records, and obsessive documentation in /docs/ subfolders. Rent-roll standardization, utility billing audits, cash-flow modeling — the methodology lives in text files the AI reads on every session. New hires read the same files. The methodology doesn’t live in anyone’s head anymore. It lives in the repo, versioned, with a git history of every time it changed. Skills formalize the pattern he’s already running.

At the more sophisticated end, teams are building orchestrator skills that analyze an incoming request, spawn isolated subagents for research, coding, verification, UI, docs, and deployment — each loading its own specialized skill — and then review all outputs against a verification skill before returning anything. One high-level request routes into parallel work that stays methodologically consistent without anyone supervising it.

The piece that makes all of this matter beyond developer workflows: skills now work identically across Claude Code, the API, and the Excel and PowerPoint sidebars. Same skill_id, same format, same behavior. An earnings analyst skill that runs beat/miss analysis and model roll-forward in the Excel sidebar when a human triggers it? That same file runs in an overnight API pipeline when an agent does. The surface doesn’t matter. The skill is the constant.

Why your best prompting work is evaporating
Most skills advice opens with: “if you repeat the same prompts, encode them as a skill.” That’s true and undersells the case by a factor of ten.

Here’s the framing that actually captures what’s happening: skills accumulate where prompts evaporate.

Think about your best AI work sessions. The session where you got an exceptional competitive analysis because you’d explained your framework in detail. The session where the financial model review was exceptional because you’d walked Claude through your specific criteria. That work was good — and then the conversation ended, and none of it transferred forward. The next time you needed the same thing, you started from zero. Your best prompting work right now is evaporating.

Skills are how your wins compound instead of disappearing. Every time you get a great result, the approach that produced it has value. A skill is what captures that value and makes it reusable: by you, by your team, by an agent running the same workflow at midnight. The build question isn’t “is this task important enough to spend time encoding?” It’s “am I okay losing this methodology every time the conversation ends?”

Frame it that way and your skill backlog gets a lot longer.

Three signals tell you something belongs in a skill, all required:

It recurs. One use is a task. Two might be coincidence. Three is a pattern. A pattern belongs in a file.

It requires methodology. Some tasks are handled cleanly by a direct prompt. Others require an approach: frameworks, decision sequences, domain-specific quality criteria, rules that only make sense if you understand why they exist. The test: would you write a methodology document for a new employee before asking them to do this? If yes, the document is a skill.

Quality depends on consistency — ad-hoc prompting produces a wide distribution of output quality, and skills raise and lock the floor. If the variability is costing you anything, it’s a skill candidate.

The highest-ROI candidates in knowledge work. Any document type you produce on cadence: client memos, board updates, deal briefs. Any analysis with a consistent quality bar: competitive intelligence, model reviews, pipeline analysis. Any quality-review process with specific criteria: contract review, deck review, code review. Any workflow where “the right way to do it here” takes a new person three months to internalize.

That last one is the team-level case, which we’ll get to. First: the mechanics.

How to build a skill for March
The description field is where most skills fail, and why they fail is specific.

A bad description:

description: Helps with competitive analysis.
Claude reads this and knows almost nothing. When does it fire? What kind of analysis? Against what quality bar? In practice, this skill either never triggers (too vague to match anything specifically) or triggers constantly on anything tangentially related to competition. Neither is what you want.

A good description for the same skill:

description: Produces structured competitive analysis memos for product,
  market, and investment research. Use when asked to analyze competitors,
  assess market position, write a competitive landscape, or evaluate
  competitive dynamics. Applies to "analyze our competitors," "who are the
  players in X market," "build a comp set," or "how do we stack up against Y."
  Returns structured memo: market definition, player profiles, positioning
  matrix, strategic implications.
What changed: document types specified, analysis frameworks named, actual trigger phrases included, output format stated. Anthropic’s own skill-creator guide is explicit: “Skills tend to under-trigger more than over-trigger. Make the description a little pushy.” When in doubt, add the phrases. The description should make Claude confident, not cautious.

One hard technical constraint that has caught enough people to become a logged GitHub issue: the description must be a single line in YAML. If a code formatter like Prettier wraps it across multiple lines, the skill silently disappears. Claude reports zero available skills, no error, no indication of what happened. Keep it on one line.

Building the body. After the frontmatter, the body of your SKILL.md is where your methodology lives. Keep it under 500 lines. Not a hard limit, but a forcing function. If you’re approaching it, you need better structure, not more instructions.

Five things every skill body needs:

The methodology, not the mechanics. Explain how to approach the work, not what to do in step 3. Give Claude your frameworks, your quality criteria, the principles behind your decisions. A skill with only procedures is brittle: when it hits an unanticipated case, it has nothing to fall back on. A skill with reasoning is durable, because reasoning generalizes where procedures don’t.

A specified output format. Not “produce a summary.” A Markdown document with these exact sections in this order. A JSON object with these fields. If the output is ambiguous, every caller, human or agent, has to interpret it, and interpretation introduces variability.

Explicit edge cases. Everything a human handles with common sense needs to be written down. What happens when required data is missing? When the input is ambiguous? When the request is partially out of scope? Write it down.

At least one example. Claude is good at pattern-matching from examples. One concrete illustration of what good output looks like dramatically improves consistency.

Lean. A 200-line skill that loads fast and fires reliably outperforms an 800-line skill where every instruction competes for attention. Move reference material to references/ and link to it. Keep the main file tight.

Build from your outputs, not your intentions. The conventional wisdom is to sit down and articulate your methodology. That produces mediocre skills, because what you think you do and what you actually do are different. Expertise lives in decisions you’ve made so many times they’ve become automatic and invisible. You can’t articulate what you can’t see.

The better approach: feed Claude ten to twenty examples of your actual best work — your best competitive analysis, your best deal brief, your best model review — and ask it to reverse-engineer the methodology into a SKILL.md. Have Claude interview you about the decisions embedded in those examples. What choices are you making? Why? What makes this one better than that one?

This is the pattern behind operators who’ve built deep methodology libraries by feeding Claude their actual work product — not descriptions of their workflows, but the real outputs, the real decisions, the real artifacts. The multifamily GP who obsessively documents every decision in /docs/ subfolders didn’t sit down and write a methodology guide. He built from evidence: raw utility reports, rent rolls, deal models. The methodology surfaced from the work itself. That’s the approach that scales, whether you’re using the formal SKILL.md format or building project infrastructure from scratch.

The failure asymmetry nobody talks about
Before getting to agent-readiness, there’s one thing you need to understand that changes the entire cost-benefit calculation for how carefully you build skills.

A vague skill in a human-directed session costs you maybe 10 to 15% of output quality. The human notices the output is off, redirects, recovers. Small cost. That recovery is invisible — it just feels like the normal back-and-forth of working with AI. You’ve been absorbing those failures so naturally you probably didn’t register them as failures.

The same vague skill in an agent pipeline doesn’t produce slightly degraded output. It produces output the downstream agent treats as correct, processes further, and hands to the next step. The error doesn’t surface where it was introduced. It surfaces six steps later, looking like a model failure, in a form that’s completely divorced from the broken skill that caused it. By that point, the bad data has been processed through multiple steps and tracing it back is hard.

The cost asymmetry: human caller, 10 to 15% quality degradation. Agent caller, potential 100% chain failure. The skills you’ve tested in interactive sessions systematically understate your failure rate in agentic contexts, because the human in the loop absorbed the failures. When you remove the human, those failures propagate.

This is why “good enough for my use” and “good enough for agents” are not the same standard. If you’re planning to put a skill into any automated pipeline, it needs to be designed for the agent-caller case, not the human-caller case. And it matters now, not in some hypothetical future, because the convergence I described at the start means skills are already running in both contexts. The Excel sidebar fires the same file as the overnight pipeline.

Four redesigns for agent callers
The trigger description needs to work without human context. An agent scanning a skill catalog doesn’t read your description thoughtfully. It matches against it. If your description doesn’t contain the phrases an orchestrating agent will generate mid-pipeline, your skill doesn’t get called. If it matches phrases it shouldn’t, the wrong skill gets applied to the wrong step. Neither failure surfaces cleanly.

Fix: treat the description as a routing table entry. Every trigger phrase that an agent might generate when it needs this skill type should appear in the description.

Output format is non-negotiable. Define it completely. Not “a structured response.” A JSON object with these specific fields. Not “a summary.” A Markdown document with these sections in this order, nothing else. The failure mode when you skip this: an agent calls your skill, gets conversational prose, can’t extract the structured data it needs for the next step, and either fails silently or hallucinates a structure. Downstream steps process the hallucinated structure as ground truth.

Structured edge case handling in practice:

## Output Format
Return a JSON object with exactly these fields:
{
  "market_definition": "string, 2-3 sentences",
  "players": [{"name": "string", "tier": "primary|secondary", "differentiator": "string"}],
  "strategic_implications": ["string", "string", "string"]
}

## Edge Cases
If the input provides fewer than three named competitors, output:
{"error": "insufficient_data", "minimum_required": 3, "provided": N}
and stop. Do not attempt analysis with insufficient input.

If the competitive frame is ambiguous, output:
{"clarification_needed": "string describing what is ambiguous"}
and stop. Do not guess at the frame.
This is deterministic edge case handling. The agent downstream knows exactly what success looks like and exactly what failure codes to expect. Compare to: “if information is missing, note it in the analysis.” That instruction produces variable behavior. The explicit JSON contract produces consistent behavior.

Composability is not optional. Agents chain skills together. A research skill’s output feeds an analysis skill’s input feeds a formatting skill’s input. Each handoff requires clean, predictable output from the previous step. A skill that produces great output in isolation but unpredictable output in sequence will break pipelines you didn’t know you were building.

When you write a skill, ask: if another agent were consuming this output, what would it need to do something useful with it? If the answer is “parse prose for structured data,” you have a composability problem. Fix the output format before the pipeline breaks it for you.

For critical validations, use scripts, not language. Language instructions are probabilistic. Scripts are deterministic. If a step absolutely must succeed before the next one runs — authentication check, required field validation, data type confirmation — put that logic in a script in scripts/ rather than relying on Claude following a language instruction. Claude follows language instructions very well. Very well is not the same as every time. And for a gate that must hold, the difference matters. This is exactly what the verification-heavy engineering patterns do: a verification skill that runs assertions, checks logs, simulates usage, and only returns output if it passes. Not instructions to verify. Actual scripts that verify.

Three tiers most teams get backwards
Personal skills eliminate setup time. That’s real. But it’s the small version.

Team skills solve a problem that has been persistent and unsolvable for most of organizational history: expertise has always been trapped inside the people who developed it. Senior practitioners carry methodology in their heads. They transfer it slowly and imperfectly — through mentorship, documentation nobody reads, and judgment that’s only available when they’re not too busy to share it. When they leave, the expertise walks out with them.

Organizations have tried to fix this with documentation for decades. It keeps not working because the mechanism is wrong. Documentation requires a person to stop what they’re doing, go find the document, open it, read it, apply it: a sequence of friction points that people routinely skip under time pressure. Skills change the mechanism entirely. The methodology fires automatically when the task arrives. Present at the moment of use, already loaded, already active.

That’s not better documentation. That’s a different transfer mechanism.

Three tiers, in order of organizational priority:

Tier 1: Standards skills. Non-negotiable consistency: brand voice, formatting rules, approved templates, compliance requirements. These belong in the org settings panel today. Since December, Team and Enterprise admins can provision skills workspace-wide from a single upload. The skill appears automatically in every member’s sidebar in Excel, PowerPoint, and Claude.ai. If your AI outputs don’t all follow the same standards, you don’t have standards. You have different people approximating standards differently with no consistency guarantee.

Tier 2: Methodology skills. How your organization actually approaches specific types of high-value work: how you run due diligence, how you structure client deliverables, how you review a contract, how you think about a pricing analysis. Built by the senior practitioners who actually know how the work gets done well, then distributed to everyone. These are the skills worth fifty hours to build carefully because they run ten thousand times. This is where expertise stops being personal and starts being institutional.

Identify the three things a new person at your organization needs three months to figure out how to do at your standard. That’s your Tier 2 backlog. The person who knows how to do those things well is exactly the right person to draft the skill. Have them encode the methodology while it’s explicit rather than waiting for it to calcify back into intuition nobody can articulate. Use the output-extraction approach: give Claude twenty examples of their best work and let it interview them about the decisions embedded in those examples.

Tier 3: Personal workflow skills. Built by individuals for their own recurring tasks. Valuable. Organizationally, least significant. Most organizations get 80% of team-level value from Tier 1 and Tier 2. If your team is building personal skills but doesn’t have organizational standards and methodology skills yet, you have the priorities backwards.

The compounding effect. Each methodology skill raises the quality floor for everyone who uses it, permanently, until someone figures out a better approach and updates the file. When the update happens, it distributes to everyone immediately, including every agent running the skill in automated pipelines. You’re not capturing expertise once. You’re building a system that improves and instantly propagates improvements everywhere.

The gap in the 500,000-skill ecosystem
Most people haven’t noticed what’s happening in the current skills ecosystem: 500,000 in the SkillsMP marketplace, and the overwhelming majority are developer tooling. Skills that make Claude Code better at writing TypeScript. Skills for Angular component patterns. Skills for Git automation, database query optimization, frontend design systems. The awesome-agent-skills repository on GitHub has 1,000-plus cross-platform skills running across Claude Code, Codex, Gemini CLI, and Cursor. The frontier-design skill alone has 277,000 installs.

This makes sense historically. Developers adopted skills first because Claude Code was where the format launched and where the technical audience was. But it leaves a massive gap.

There is no shared library of skills for knowledge work. Not for competitive analysis. Not for financial model review. Not for deal memos or client deliverables or legal contract review or marketing methodology. Anthropic launched an open-source knowledge-work-plugins repo in late January with 11 starter plugins covering roles like sales, finance, legal, product, and operations. That’s a starting point, not a library. The role-level starting points exist. The domain-specific methodology skills that encode how a particular type of organization does this specific type of work at a high standard: those don’t exist yet.

Simon Willison wrote in October that skills were “maybe a bigger deal than MCP.” Because they’re just text files that travel everywhere. Because the methodology they encode is more durable than any integration. That observation was more right than he knew, because the standard has since spread to Excel, PowerPoint, VS Code, GitHub, M365, and two competing labs, all reading the same file format.

One consultant ran a full deck, summary, and recommendations in under thirty minutes using skills inside Claude. Work that would normally take a traditional vendor weeks. The Buyside AI community showed the same pattern: skills plus Excel and PowerPoint letting non-technical teams get finished deliverables at their standard, without explaining their methodology on every task. Half the audience had barely used Claude before. The integration made the encoded-methodology part obvious in a way chat never had.

That gap is what I’ve been building toward. I’ve been creating skills for the work I do — advising organizations on agentic systems, building context layers for complex recurring workflows, watching the same failure patterns repeat in organizations that haven’t encoded their methodology. The community skills repository is a practitioner library for knowledge work, organized by workflow type, with an agent-readability bar applied before anything goes in. Every skill must have an explicit output format, explicit edge case handling, a trigger description written as a routing signal, and composable output structure.

The first batch: competitive analysis, financial model review, deal memo drafting, research synthesis, and meeting synthesis. These are the workflow types that fail most consistently in production agentic contexts — built for human callers, never redesigned for the agent-caller case. Rebuilt here against the right standard.

If you’re running Open Brain as your personal knowledge store, you can pull community skills directly into your context with a single command. The methodology commons becomes part of your infrastructure, available to your agents automatically. The repo is live at github.com/NateBJones-Projects/OB1/skills. Contribution guide is there. If you have a skill that meets the standard, submit it.

What to do this week
If you have 30 minutes: Run the skill backlog audit. Pull up your last 30 conversations with Claude. Which prompts did you write for the third time this month? That’s your backlog. Pick the one with the highest quality variance, where the output is sometimes great and sometimes not, and build it first.

If you have two hours: Use the output-extraction method. Collect ten to twenty examples of your best work in that domain. Feed them to Claude, ask it to identify the decisions you’re making and reverse-engineer them into a SKILL.md. Have Claude interview you about the choices embedded in the examples. Test the result with vague, realistic prompts, not engineered test cases, but the kind of half-specified requests that actually arrive. Fix wherever the output drifts.

If you’re deploying to agents: Audit every skill in your pipeline against the four criteria. Does the description contain the phrases an orchestrating agent will generate? Is the output format completely specified? Are edge cases explicit with defined failure modes? Is the output composable — could another skill consume it cleanly without parsing prose? Any “no” is a pipeline failure waiting to surface six steps downstream looking like a model problem.

If you manage a team: Identify your Tier 2 backlog. Three things a new person needs three months to learn to do at your standard. Have your senior people build skills from their actual output examples, not from their articulated intentions. Push the result to your org settings panel. Watch how fast new people get up to speed.

If you run an organization: The question has moved. Not “should we use skills?” but “which of our skills are organizational infrastructure that need versioning and governance, and which are personal configuration that individuals own?” The institutional ones, the methodology skills that encode how your organization approaches its highest-value work, belong in a governed library, not scattered across individual accounts. Those are the ones that walk out the door when people leave. Those are the ones agents need to run correctly at 2am. Build them like infrastructure because that’s what they are.

The pace is the point
Five months. That’s the entire timeline from internal feature to cross-platform standard to organizational infrastructure running in Excel, PowerPoint, and M365.

The organizations building skills libraries now will have compounding advantages over the ones that haven’t, because their agents — across every surface, every pipeline, every platform — run on encoded institutional methodology. Everyone else’s agents are inferring standards from whatever context the task happens to provide.

Your methodology is evaporating right now — every session, every conversation that ends and takes your best work with it. Skills are how it compounds instead.

The format is boring. The leverage is not.