---
name: meta-travel-planner
description: "Use this meta-skill instead of answering directly when the user needs a trip plan, travel itinerary, business-trip schedule, or day-by-day travel brief that benefits from multi-skill orchestration across preference inference, weather, place search, constraint extraction, itinerary drafting, variants, and optional artifact guidance."
kind: meta
meta_priority: 50
always: false
final_text_mode: "step:final_plan"
triggers:
  - "travel plan"
  - "trip plan"
  - "trip itinerary"
  - "travel itinerary"
  - "day-by-day travel"
  - "旅游计划"
  - "出差行程"
  - "行程安排"
  - "规划行程"
  - "帮我安排"
  - "怎么玩"
  - "做个行程"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
composition:
  steps:
    - id: trip_collect
      kind: user_input
      clarify:
        mode: form
        intro: |
          在开始规划之前，请先确认 4 件事 —— 我会用它生成完整行程。
        fields:
          - name: destination
            type: string
            required: true
            prompt: "目的地（城市或地区）"
            max_chars: 80
          - name: days
            type: int
            required: true
            min: 1
            max: 30
            prompt: "行程天数（1–30）"
          - name: party_size
            type: int
            required: true
            min: 1
            max: 20
            prompt: "出行人数（1–20）"
          - name: budget
            type: enum
            choices: [budget, mid, premium]
            default: mid
            prompt: "预算档次（budget / mid / premium）"
        cancel_keywords: ["算了", "取消", "cancel", "stop"]
        timeout_hours: 24
    - id: trip_preferences
      kind: llm_chat
      depends_on: [trip_collect]
      with:
        system: "You expand user-confirmed travel facts into a structured planning contract."
        task: |
          Expand the user-confirmed travel facts into a full planning contract.

          User-confirmed facts:
          DESTINATION: {{ inputs.collected.trip_collect.destination | xml_escape }}
          DAYS: {{ inputs.collected.trip_collect.days }}
          PARTY: {{ inputs.collected.trip_collect.party_size }}
          BUDGET: {{ inputs.collected.trip_collect.budget }}

          Original user request (context only, do NOT override confirmed facts):
          {{ inputs.user_message | xml_escape | truncate(1200) }}

          Return exactly:
          DESTINATION: <city/region>
          DATES: <duration or date range>
          PARTY: <party size/type>
          BUDGET: <budget level>
          PACE: <relaxed|balanced|packed>
          INTERESTS:
            - <interest>
          CONSTRAINTS:
            - <constraint or assumption>
    - id: weather
      kind: skill_exec
      skill: weather
      depends_on: [trip_preferences]
      with:
        location: "{{ outputs.trip_preferences | truncate(512) }}"
        days: 3
        max_chars: 2200
    - id: poi
      kind: skill_exec
      skill: multi-search-engine
      depends_on: [trip_preferences]
      with:
        query: "{{ outputs.trip_preferences | truncate(512) }} sights restaurants transport hours neighborhoods"
        engines: [brave, duckduckgo]
        max_results: 15
    - id: constraints
      kind: llm_chat
      depends_on: [weather, poi]
      with:
        system: "You convert weather and search results into itinerary constraints."
        task: |
          Extract itinerary constraints from weather and POI results: opening
          hours, transit time assumptions, weather risks, neighborhoods to
          group together, and any likely booking constraints.

          Preferences:
          {{ outputs.trip_preferences | truncate(1200) }}

          Weather:
          {{ outputs.weather | truncate(2000) }}

          POI search:
          {{ outputs.poi | truncate(6000) }}
    - id: itinerary
      kind: llm_chat
      depends_on: [constraints]
      with:
        system: "You write complete, practical travel itineraries. Return only the itinerary."
        task: |
          Build the primary day-by-day itinerary. It must be complete enough
          to use without reading any later step.

          Include:
          - assumptions
          - one section per day with morning / afternoon / evening
          - neighborhood grouping and transit notes
          - food suggestions
          - rain-aware risks and substitutions
          - rough budget notes

          Trip preferences:
          {{ outputs.trip_preferences | truncate(1200) }}

          Weather forecast:
          {{ outputs.weather | truncate(2000) }}

          POI search:
          {{ outputs.poi | truncate(5000) }}

          Constraints:
          {{ outputs.constraints | truncate(3000) }}
    - id: final_plan
      kind: llm_chat
      depends_on: [itinerary, constraints, weather, poi]
      with:
        system: "You assemble complete travel plans for users. Return only the final answer."
        task: |
          Assemble the complete travel product. Do not return only variants.
          Do not include process commentary.
          Return every required section. Keep the whole answer compact enough
          to fit in one model response: 4,500-6,500 characters is preferred.
          If space is tight, shorten day descriptions before omitting the
          variants, evidence, next-step, or artifact sections.

          Required sections:
          1. Assumptions
          2. Primary itinerary matching the requested or inferred trip length
          3. Weather-aware risks and rain backups
          4. Variants
          5. Budget and booking notes
          6. Evidence and source notes
          7. Next steps

          Preserve concrete timings, neighborhoods, transit grouping, food
          ideas, weather constraints, and budget constraints. Do not open with
          "I researched" or imply live verification unless a tool result is
          shown in the evidence notes. Keep each day to 5-7 highly actionable
          bullets or a compact schedule. Include:
          - a Route spine line for each day, e.g. Neighborhood A -> B -> C
          - no more than 2-3 main anchors per day unless the user requested a
            packed pace
          - an explicit pacing note: relaxed/balanced/packed and what to skip
            if tired
          - transit-coherent neighborhood adjacency; avoid cross-city zigzags
          - relaxed version
          - efficient/packed version
          - bad-weather backup
          - weather switch points: if rain/heavy heat, swap X for Y
          - rough daily budget notes
          - specific checks before booking, including opening-hours checks,
            timed-entry reservations, and transit-pass choice
          - mark specific restaurants, opening hours, and seasonal events as
            "verify before booking" unless they came from explicit search
            evidence in this run
          - a short note that a styled HTML itinerary can be generated only if
            the user explicitly asks for a file

          If search or weather evidence is thin, state assumptions plainly
          instead of inventing sources. Include map/search links only as
          plain URLs when useful. Use the words Evidence, Source notes,
          Reference checks, Next steps, Verify, HTML, and Report
          only where they fit naturally in the final sections.

          Itinerary:
          {{ outputs.itinerary | truncate(7000) }}

          Constraint notes:
          {{ outputs.constraints | truncate(2500) }}

          Weather evidence:
          {{ outputs.weather | truncate(1600) }}

          POI/source notes:
          {{ outputs.poi | truncate(2000) }}
---

# Travel Planner (Meta-Skill)

Weather + POI/restaurant/transport search + constraints + a complete itinerary
with variants. The default answer is a complete travel plan; HTML export is an
optional handoff when the user explicitly asks for a file.

## Fallback

Manually call weather, multi-search-engine, summarize. If the user explicitly
asks for HTML export, ask the LLM to write a styled `travel-itinerary.html`
and `publish_artifact` it.
