/**
 * dsh-screen-agent-linux — screen vision and synthetic input for DSH on Linux.
 *
 * Phase 1: three read-only tools.
 *   screen_look     screenshot the full desktop and return it as an image
 *   screen_zoom     crop a region at native resolution, for reading small text
 *   screen_windows  list managed top-level windows in stacking order
 *
 * Vision. The provider resizes every image to roughly an 800x800 equivalent
 * (640,000 px), so a full 2560x1600 screenshot reaches the model at about
 * 1011x632 and fine detail is gone before it is ever seen. `screen_zoom` exists
 * for that: it crops from a *native-resolution* grab, and a crop at or below
 * 640k px arrives losslessly. Coordinates are always normalized (0..1) because
 * fractions survive every resize while pixels do not.
 *
 * Images reach model context through the durable attachment store and are
 * projected by `output.render` as `{ type: 'image' }`.
 *
 * Deliberately import-free at runtime: no build step and no dependency on any
 * dsh package, which removes a whole class of load failures. The sidecar is
 * plain Python plus ctypes (Pillow for capture), and image payloads come back
 * inline as base64 so concurrent calls never race on a temp file.
 *
 * Rewritten for Linux after `Arthur303Author/dsh-plugins`'s Windows
 * `dsh-screen-agent` (BSD-3-Clause). The behaviour that survived is the part
 * worth keeping: staged tool release, normalized coordinates, and inline
 * base64 through the attachment store. The Win32 layer is gone; the
 * measurements behind these decisions are in probe/REPORT.md.
 */

import { execFile } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'dsh-screen-agent-linux'

/**
 * Services this plugin needs before it can publish tools.
 *
 * `tools` is a hard dependency: every registration goes through it.
 * Everything else (`attachments`, `llm`) is taken with `ctx.get()` and handled
 * when absent, so a leaner profile still gets working tools.
 */
export const inject = ['tools']

const HERE = dirname(fileURLToPath(import.meta.url))
const SIDECAR = join(HERE, 'screen_tools.py')

/** Provider-side per-image visual budget, mirrored by the sidecar. */
export const IMAGE_PIXEL_BUDGET = 640000

// ---------------------------------------------------------------------------
// Python / sidecar plumbing
// ---------------------------------------------------------------------------

let pythonPromise

function pythonCandidates() {
  return [
    process.env.DSH_SCREEN_AGENT_PYTHON,
    'python3',
    'python',
  ].filter((value) => typeof value === 'string' && value.length > 0)
}

/**
 * Probe one interpreter asynchronously; never blocks the host event loop.
 *
 * The check covers what the sidecar actually needs: Pillow for capture and the
 * ctypes X11 layer, which is imported through the sidecar's own directory.
 */
function probeCandidate(candidate) {
  return new Promise((resolve) => {
    let settled = false
    const settle = (ok) => {
      if (!settled) {
        settled = true
        resolve(ok)
      }
    }
    const probe = `import sys; sys.path.insert(0, ${JSON.stringify(HERE)}); import PIL; from linux import x11`
    try {
      const child = execFile(candidate, ['-c', probe], { timeout: 8000 }, (error) => {
        settle(error === null || error === undefined)
      })
      child.on('error', () => settle(false))
    } catch {
      settle(false)
    }
  })
}

/** Resolve (once, cached) an interpreter that can run the sidecar. */
function ensurePython() {
  if (pythonPromise === undefined) {
    pythonPromise = (async () => {
      for (const candidate of pythonCandidates()) {
        if (await probeCandidate(candidate)) return candidate
      }
      return null
    })()
  }
  return pythonPromise
}

/**
 * Run one sidecar request.
 * @param request - JSON request; the sidecar answers with exactly one JSON object.
 * @param signal - caller cancellation, forwarded to the child process.
 */
async function runSidecar(request, signal) {
  const python = await ensurePython()
  if (python === null) {
    throw new Error(
      'no Python with Pillow and a working X11 layer found; set DSH_SCREEN_AGENT_PYTHON '
      + 'to a python3 that can "import PIL" on this display',
    )
  }
  if (signal?.aborted === true) throw new Error('the call was canceled before the sidecar started')

  return await new Promise((resolve, reject) => {
    const child = execFile(python, [SIDECAR], {
      timeout: 60000,
      // Inline base64 payloads: a 640k-px PNG is well under 1 MiB, so 64 MiB
      // leaves generous headroom for several concurrent calls.
      maxBuffer: 64 * 1024 * 1024,
      encoding: 'utf8',
    }, (error, stdout, stderr) => {
      if (signal !== undefined) signal.removeEventListener('abort', onAbort)
      if (error !== null && error !== undefined && error.killed === true) {
        reject(new Error('the screen sidecar timed out after 60s'))
        return
      }
      const out = String(stdout ?? '').trim()
      if (out.length === 0) {
        const detail = String(stderr ?? '').trim() || (error?.message ?? 'no output')
        reject(new Error(`the screen sidecar produced no output: ${detail.slice(0, 500)}`))
        return
      }
      let parsed
      try {
        parsed = JSON.parse(out)
      } catch {
        reject(new Error(`the screen sidecar returned unreadable output: ${out.slice(0, 500)}`))
        return
      }
      if (parsed?.ok !== true) {
        reject(new Error(String(parsed?.error ?? 'the screen sidecar reported an unknown failure')))
        return
      }
      resolve(parsed)
    })

    const onAbort = () => child.kill()
    if (signal !== undefined) signal.addEventListener('abort', onAbort, { once: true })
    child.stdin.on('error', () => {})       // the child may exit before reading stdin
    child.stdin.end(JSON.stringify(request), 'utf8')
  })
}

// ---------------------------------------------------------------------------
// Screenshot -> model context
// ---------------------------------------------------------------------------

function shotPath() {
  const base = process.env.DSH_HOME ?? join(homedir(), '.dsh')
  const dir = join(base, 'screen-agent')
  mkdirSync(dir, { recursive: true })
  return join(dir, 'desktop.png')
}

function round(value, digits = 4) {
  return Number(Number(value).toFixed(digits))
}

/** The note is an operating instruction for the model, not a log line. */
function describe(meta, action) {
  if (action === 'zoom') {
    const width = round(meta.nx1 - meta.nx0)
    const height = round(meta.ny1 - meta.ny0)
    const quality = meta.lossless
      ? 'full native resolution, nothing lost'
      : `downscaled x${round(meta.scale)} because the rectangle exceeded the ${IMAGE_PIXEL_BUDGET} px budget`
    return `Crop of the ${meta.desktopWidth}x${meta.desktopHeight} desktop: nx ${round(meta.nx0)}-${round(meta.nx1)}, `
      + `ny ${round(meta.ny0)}-${round(meta.ny1)} (${meta.cropWidth}x${meta.cropHeight} px, ${quality}). `
      + `To address a spot inside this view, convert its local fraction back to a desktop fraction: `
      + `nx = ${round(meta.nx0)} + nxLocal*${width}, ny = ${round(meta.ny0)} + nyLocal*${height}.`
  }
  if (action === 'capture') {
    return `Full desktop ${meta.desktopWidth}x${meta.desktopHeight} (all monitors), `
      + `returned at ${meta.imageWidth}x${meta.imageHeight}`
      + `${meta.lossless ? '' : ` (downscaled x${round(meta.scale)} to fit the ${IMAGE_PIXEL_BUDGET} px budget)`}. `
      + 'Address spots on the desktop as fractions of it: 0,0 is top-left, 1,1 is bottom-right.'
  }
  return `screen result (${action})`
}

/** Decode the inline PNG payload from a sidecar response. */
function decodePayload(meta) {
  const base64 = meta.pngBase64
  if (typeof base64 !== 'string' || base64.length === 0) {
    throw new Error('the screen sidecar returned no image payload')
  }
  const bytes = Buffer.from(base64, 'base64')
  if (bytes.length === 0) throw new Error('the screen sidecar returned an empty image payload')
  return bytes
}

/** Text-only fallback: the screenshot still lands on disk and is named. */
function textFallback(bytes, note, reason) {
  const out = shotPath()
  writeFileSync(out, bytes)
  return { kind: 'text', note: `${note} ${reason} The screenshot was saved to ${out}.`, path: out }
}

/** Whether the active model route declares image input (so an image is useful). */
async function routeAcceptsImages(ctx, exec) {
  try {
    const llm = ctx.get('llm')
    if (llm === undefined || typeof llm.resolveModelInfo !== 'function') return true
    const routed = exec?.agent?.session?.requestHeader?.()?.config
    const provider = routed?.provider ?? exec?.agent?.options?.provider
    const model = routed?.model ?? exec?.agent?.options?.model
    if (provider === undefined || model === undefined) return true
    const info = await llm.resolveModelInfo(provider, model, exec?.signal)
    if (info?.inputModalities === undefined) return true
    return info.inputModalities.includes('image') === true
  } catch {
    // A probe failure must never turn into a lost screenshot: assume images work.
    return true
  }
}

/**
 * Run one image-producing sidecar action and turn it into a canonical tool value.
 * @returns `{ kind: 'image', image, note }` or a text fallback carrying the path.
 */
/** Turn a sidecar response that already carries a PNG into a canonical tool value. */
async function imageFromMeta(ctx, exec, meta, note, name) {
  const bytes = decodePayload(meta)
  const attachments = ctx.get('attachments')
  if (attachments === undefined) {
    return textFallback(bytes, note, 'No attachment store is mounted, so no image reached model context.')
  }
  if (!(await routeAcceptsImages(ctx, exec))) {
    return textFallback(bytes, note, 'The active model route declares no image input, so no image reached model context.')
  }

  const refs = await attachments.saveImages([{
    data: new Uint8Array(bytes),
    mediaType: 'image/png',
    name: name ?? 'screen.png',
  }])
  return { kind: 'image', image: refs[0], note }
}

/**
 * Run one image-producing sidecar action and turn it into a canonical tool value.
 * @returns `{ kind: 'image', image, note }` or a text fallback carrying the path.
 */
async function imageValue(ctx, exec, request, describeFn = describe) {
  const meta = await runSidecar({ ...request, inline: true }, exec?.signal)
  return await imageFromMeta(ctx, exec, meta, describeFn(meta, request.action),
    request.name ?? 'screen.png')
}

// ---------------------------------------------------------------------------
// Shared output contract
// ---------------------------------------------------------------------------

const SCREENSHOT_OUTPUT = {
  schema: {
    type: 'object',
    properties: {
      kind: { type: 'string', enum: ['image', 'text'] },
      note: { type: 'string' },
      path: { type: 'string' },
      image: { type: 'object', additionalProperties: true },
    },
    required: ['kind', 'note'],
    additionalProperties: false,
  },
  render: (_args, value) => {
    const blocks = []
    if (value?.kind === 'image' && value.image !== undefined) {
      blocks.push({ type: 'image', attachment: value.image })
    }
    blocks.push({ type: 'text', text: String(value?.note ?? '') })
    return blocks
  },
}

const TEXT_OUTPUT = {
  schema: {
    type: 'object',
    properties: { text: { type: 'string' } },
    required: ['text'],
    additionalProperties: false,
  },
  render: (_args, value) => [{ type: 'text', text: String(value?.text ?? '') }],
}

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

const lookTool = {
  name: 'screen_look',
  description: 'Screenshot the full desktop (all monitors) and return it as an image. '
    + 'Address spots on it as fractions of the desktop: nx 0..1 left-to-right, ny 0..1 top-to-bottom. '
    + 'A full desktop is downscaled to fit the image budget, so small text may be unreadable — '
    + 'use screen_zoom on the region instead of guessing from this view.',
  parameters: { type: 'object', properties: {}, required: [], additionalProperties: false },
  output: SCREENSHOT_OUTPUT,
  async execute(_args, exec, ctx) {
    return await imageValue(ctx, exec, { action: 'capture', name: 'desktop.png' })
  },
}

const zoomTool = {
  name: 'screen_zoom',
  description: 'Crop a region of the desktop at NATIVE resolution and return it as an image. '
    + 'Use it to read small text, read a value, or find a small control that the full screenshot renders too coarsely. '
    + `A crop of at most ${IMAGE_PIXEL_BUDGET} px arrives losslessly; larger ones are downscaled. `
    + 'All four edges are fractions of the desktop: nx0/ny0 is the top-left of the crop, nx1/ny1 the bottom-right.',
  parameters: {
    type: 'object',
    properties: {
      nx0: { type: 'number', description: 'Left edge of the crop, as a fraction of desktop width (0..1).' },
      ny0: { type: 'number', description: 'Top edge of the crop, as a fraction of desktop height (0..1).' },
      nx1: { type: 'number', description: 'Right edge of the crop, as a fraction of desktop width (0..1).' },
      ny1: { type: 'number', description: 'Bottom edge of the crop, as a fraction of desktop height (0..1).' },
    },
    required: ['nx0', 'ny0', 'nx1', 'ny1'],
    additionalProperties: false,
  },
  output: SCREENSHOT_OUTPUT,
  async execute(args, exec, ctx) {
    return await imageValue(ctx, exec, {
      action: 'zoom',
      nx0: args.nx0,
      ny0: args.ny0,
      nx1: args.nx1,
      ny1: args.ny1,
      name: 'zoom.png',
    })
  },
}

const windowsTool = {
  name: 'screen_windows',
  description: 'List the visible top-level windows of the desktop in stacking order (topmost first), '
    + 'with each window\'s title, size, position, WM class and process id. '
    + 'Use it to find out what is open and where a window sits before looking at or pointing at anything, '
    + 'and to name a target window precisely instead of guessing from a screenshot.',
  parameters: { type: 'object', properties: {}, required: [], additionalProperties: false },
  output: TEXT_OUTPUT,
  async execute(_args, exec) {
    const meta = await runSidecar({ action: 'windows' }, exec?.signal)
    const lines = Array.isArray(meta.lines) ? meta.lines : []
    const header = `Desktop ${meta.desktopWidth}x${meta.desktopHeight}; `
      + `${meta.count} visible top-level window(s), topmost first:`
    const footer = 'Address positions as fractions of the desktop '
      + '(nx = x / desktopWidth, ny = y / desktopHeight).'
    return { text: [header, ...lines, footer].join('\n') }
  },
}

// ---------------------------------------------------------------------------
// Input tools
// ---------------------------------------------------------------------------

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/**
 * Close the see -> act loop: describe what happened, then show the result.
 *
 * The screenshot is a SEPARATE sidecar call, which is why input actions offer
 * `settle`: when an action is expected to repaint the screen, letting the action
 * itself wait establishes the baseline in the same process. A separate wait call
 * cannot -- by the time its process starts, a fast repaint is already part of
 * its baseline (measured: a resize moving 4.8% of the fingerprint read as no
 * change at all that way).
 */
async function withCapture(ctx, exec, meta, actionText, capture) {
  const parts = [actionText]
  if (meta?.settled === true) {
    parts.push('The screen settled afterwards.')
  } else if (meta?.settled === false) {
    parts.push('The screen was still changing when the settle timeout expired.')
  }
  if (capture === false) return { kind: 'text', note: parts.join(' ') }

  await sleep(250)
  const shot = await imageValue(ctx, exec, { action: 'capture', name: 'desktop.png' })
  return { ...shot, note: `${parts.join(' ')} ${shot.note}` }
}

/** Key-combination parameter shared by the text-capable tools. */
const KEY_PARAM = {
  type: 'array',
  items: { type: 'string' },
  description: 'Key combinations pressed in order, e.g. ["esc"], ["ctrl+z"], ["f3", "enter"]. '
    + 'A modifier may prefix a key with "+": ctrl, shift, alt, super. Letters, digits, F1-F24, '
    + 'and names like esc/tab/enter/space/home/end/delete/arrows are accepted.',
}

const SETTLE_PARAM = {
  type: 'boolean',
  description: 'Wait for the screen to stop changing before returning; useful when the action should '
    + 'open, close, or load something.',
}

const CAPTURE_PARAM = {
  type: 'boolean',
  description: 'Return a verification screenshot; defaults to true.',
}

const moveTool = {
  name: 'screen_move',
  description: 'Move the mouse pointer to a point on the desktop, pressing nothing, then return a screenshot. '
    + 'It is deliberately separate from screen_click: a click that moves the pointer can change what is under it '
    + '(popup menus reposition), so the reliable order is screen_move, look at where the pointer landed, then '
    + 'screen_click with no coordinates to press exactly there with no movement in between.',
  parameters: {
    type: 'object',
    properties: {
      nx: { type: 'number', description: 'Horizontal position, as a fraction of desktop width (0..1).' },
      ny: { type: 'number', description: 'Vertical position, as a fraction of desktop height (0..1).' },
      capture: CAPTURE_PARAM,
    },
    required: ['nx', 'ny'],
    additionalProperties: false,
  },
  output: SCREENSHOT_OUTPUT,
  async execute(args, exec, ctx) {
    const meta = await runSidecar({ action: 'move', nx: args.nx, ny: args.ny }, exec?.signal)
    let text = `Cursor placed at desktop (${meta.cursorX},${meta.cursorY}); nothing was pressed.`
    if (meta.windowProtected === true) {
      text += ` The cursor is over a protected window (${meta.windowUnderCursor}), so clicking there would be refused.`
    }
    return await withCapture(ctx, exec, meta, text, args.capture)
  },
}

const clickTool = {
  name: 'screen_click',
  description: 'Press a mouse button, then return a screenshot. Pass nx/ny to move there first, or omit them '
    + '(or follow a screen_move with move:false) to press exactly where the pointer already is, with no movement '
    + 'between measuring and clicking. A click that would land on a window driving this agent is refused.',
  parameters: {
    type: 'object',
    properties: {
      nx: { type: 'number', description: 'Horizontal position to move to first, as a fraction of desktop width. Omit to press where the pointer already is.' },
      ny: { type: 'number', description: 'Vertical position to move to first, as a fraction of desktop height. Omit to press where the pointer already is.' },
      move: { type: 'boolean', description: 'Move the pointer to nx/ny before pressing; defaults to true. Set false to press in place after a screen_move, so nothing shifts in between.' },
      button: { type: 'string', enum: ['left', 'right', 'middle'], description: 'Mouse button; defaults to left.' },
      clicks: { type: 'integer', description: 'Click count; defaults to 1. Use 2 for a double click.' },
      settle: SETTLE_PARAM,
      capture: CAPTURE_PARAM,
    },
    required: [],
    additionalProperties: false,
  },
  output: SCREENSHOT_OUTPUT,
  async execute(args, exec, ctx) {
    const request = {
      action: 'click',
      button: args.button ?? 'left',
      clicks: args.clicks ?? 1,
    }
    // Forward whichever halves were given: the sidecar rejects a half-supplied
    // coordinate rather than silently pressing wherever the pointer happens to be.
    if (args.nx !== undefined) request.nx = args.nx
    if (args.ny !== undefined) request.ny = args.ny
    if (args.nx !== undefined && args.ny !== undefined) request.move = args.move !== false
    if (args.settle === true) request.settle = true

    const meta = await runSidecar(request, exec?.signal)
    const where = meta.inPlace === true
      ? `in place at the current pointer (${meta.cursorX},${meta.cursorY})`
      : `${meta.movedCursor === true ? 'after moving to' : 'at'} desktop (${meta.cursorX},${meta.cursorY})`
    const target = meta.windowTitle ? `, over "${meta.windowTitle}"` : ''
    const text = `Clicked ${meta.button} x${meta.clicks} ${where}${target}.`
    return await withCapture(ctx, exec, meta, text, args.capture)
  },
}

const keyTool = {
  name: 'screen_key',
  description: 'Send key combinations to the focused window. Prefer a keyboard shortcut over clicking whenever one '
    + 'exists: it does not depend on where anything is on screen, and it cannot miss. '
    + 'Each entry is one combination, e.g. ["esc"], ["ctrl+z"], ["f3", "enter"].',
  parameters: {
    type: 'object',
    properties: {
      keys: KEY_PARAM,
      settle: SETTLE_PARAM,
      capture: CAPTURE_PARAM,
    },
    required: ['keys'],
    additionalProperties: false,
  },
  output: SCREENSHOT_OUTPUT,
  async execute(args, exec, ctx) {
    const request = { action: 'key', keys: args.keys }
    if (args.settle === true) request.settle = true
    const meta = await runSidecar(request, exec?.signal)
    const pressed = (meta.keys ?? []).map((combo) => `"${combo}"`).join(', ')
    const text = `Pressed ${pressed} to ${meta.focusedWindow}.`
    return await withCapture(ctx, exec, meta, text, args.capture)
  },
}

const typeTool = {
  name: 'screen_type',
  description: 'Send key combinations and/or text to the focused window. ASCII text goes through the keyboard; '
    + 'anything else (accented letters, CJK, emoji) is delivered through the accessibility text interface, because '
    + 'synthesized key events cannot type non-ASCII on X11 at all. That path needs the target field to hold focus '
    + 'and to expose an editable text interface, and it fails loudly rather than silently dropping characters.',
  parameters: {
    type: 'object',
    properties: {
      text: { type: 'string', description: 'Text to type. Optional when keys or enter is given.' },
      keys: KEY_PARAM,
      enter: { type: 'boolean', description: 'Press Enter after any text; defaults to false.' },
      settle: SETTLE_PARAM,
      capture: CAPTURE_PARAM,
    },
    required: [],
    additionalProperties: false,
  },
  output: SCREENSHOT_OUTPUT,
  async execute(args, exec, ctx) {
    const request = { action: 'type' }
    if (args.text !== undefined) request.text = args.text
    if (args.keys !== undefined) request.keys = args.keys
    if (args.enter === true) request.enter = true
    if (args.settle === true) request.settle = true

    const meta = await runSidecar(request, exec?.signal)
    const parts = []
    if (meta.characters > 0) parts.push(`${meta.characters} character(s) via ${meta.delivery}`)
    if (meta.keysPressed > 0) parts.push(`${meta.keysPressed} key combination(s)`)
    if (meta.enter === true) parts.push('Enter')
    const text = `Sent ${parts.length > 0 ? parts.join(' + ') : 'nothing'} to ${meta.focusedWindow}.`
    return await withCapture(ctx, exec, meta, text, args.capture)
  },
}

const waitTool = {
  name: 'screen_wait',
  description: 'Wait for the screen to change, or to stop changing, instead of sleeping for a guessed time. '
    + 'It compares a coarse 64x40 digest of the desktop, so it notices a window opening or a page loading but not a '
    + 'single character changing in a label. When the wait must start together with the thing that causes the '
    + 'change, use the "settle" option on that action instead: it takes its baseline inside the same call.',
  parameters: {
    type: 'object',
    properties: {
      for: {
        type: 'string',
        enum: ['change', 'stable'],
        description: 'What to wait for; defaults to "change".',
      },
      timeoutMs: { type: 'integer', description: 'Give up after this long; defaults to 10000, maximum 120000.' },
      intervalMs: { type: 'integer', description: 'Sampling interval; defaults to 250.' },
    },
    required: [],
    additionalProperties: false,
  },
  output: TEXT_OUTPUT,
  async execute(args, exec) {
    const request = { action: 'wait' }
    if (args.for !== undefined) request.for = args.for
    if (args.timeoutMs !== undefined) request.timeoutMs = args.timeoutMs
    if (args.intervalMs !== undefined) request.intervalMs = args.intervalMs

    const meta = await runSidecar(request, exec?.signal)
    let text
    if (meta.settled === true) {
      text = meta.mode === 'change'
        ? `The screen changed (${round(meta.changeRatio)} of it) after ${meta.elapsedMs}ms.`
        : `The screen stopped changing after ${meta.elapsedMs}ms.`
    } else {
      text = meta.mode === 'change'
        ? `The screen did not change within ${meta.elapsedMs}ms.`
        : `The screen was still changing after ${meta.elapsedMs}ms.`
    }
    return { text }
  },
}

// ---------------------------------------------------------------------------
// Element and window tools
// ---------------------------------------------------------------------------

const WINDOW_PARAM = {
  oneOf: [{ type: 'integer' }, { type: 'string' }],
  description: 'Window to act on: an index from screen_windows, a title substring, or a hex '
    + 'window id like 0x03400003.',
}

const elementsTool = {
  name: 'screen_elements',
  description: 'List a window\'s elements from the OS accessibility tree, with the actions each element '
    + 'advertises and its live state. Prefer this over reading a screenshot whenever the application exposes '
    + 'a tree: it is immune to scaling, theming and layout drift, costs no image tokens, and pairs with '
    + 'screen_act to operate an element directly. The tree is sampled until two consecutive reads agree, '
    + 'because it is built lazily -- a single early read returns only the window shell and none of the page. '
    + 'Custom-drawn UIs (Blender and similar) expose nothing, and then this reports an empty tree: fall back '
    + 'to screen_zoom there.',
  parameters: {
    type: 'object',
    properties: {
      window: WINDOW_PARAM,
      filter: { type: 'string', description: 'Only return elements whose name contains this text.' },
      limit: { type: 'integer', description: 'Maximum elements to read; defaults to 200.' },
    },
    required: ['window'],
    additionalProperties: false,
  },
  output: TEXT_OUTPUT,
  async execute(args, exec) {
    const request = { action: 'elements', window: args.window }
    if (args.filter !== undefined) request.filter = args.filter
    if (args.limit !== undefined) request.limit = args.limit
    const meta = await runSidecar(request, exec?.signal)
    return { text: String(meta.text ?? '') }
  },
}

const actTool = {
  name: 'screen_act',
  description: 'Operate an element through its own accessibility interface: invoke (click), set_value, '
    + 'insert_text, toggle, select, expand, collapse, or focus. It measures no coordinates, reads no '
    + 'screenshot, and does not move the pointer -- measured on Linux it does not even take focus, so the '
    + 'target window can stay in the background. Identify the element with the name/role from screen_elements, '
    + 'and pass occurrence when several elements share them.',
  parameters: {
    type: 'object',
    properties: {
      window: WINDOW_PARAM,
      elementAction: {
        type: 'string',
        enum: ['invoke', 'set_value', 'insert_text', 'toggle', 'select', 'expand', 'collapse', 'focus', 'describe'],
        description: 'What to do with the element.',
      },
      name: { type: 'string', description: 'Accessible name of the element, as shown by screen_elements.' },
      role: { type: 'string', description: 'Accessible role, e.g. "push button", "check box", "text", "menu item".' },
      automationId: { type: 'string', description: 'Accessible id, when the element has one.' },
      occurrence: { type: 'integer', description: 'Which match to act on when several share the name and role; defaults to 1.' },
      value: { type: 'string', description: 'Text for set_value or insert_text.' },
      keepFocus: { type: 'boolean', description: 'Hand focus back to the previously focused window afterwards; defaults to true.' },
    },
    required: ['window', 'elementAction'],
    additionalProperties: false,
  },
  output: TEXT_OUTPUT,
  async execute(args, exec) {
    const request = { action: 'act', window: args.window, elementAction: args.elementAction }
    for (const key of ['name', 'role', 'automationId', 'occurrence', 'value', 'keepFocus']) {
      if (args[key] !== undefined) request[key] = args[key]
    }
    const meta = await runSidecar(request, exec?.signal)

    const resolved = meta.resolved ?? {}
    const target = [resolved.role ?? meta.element?.role, resolved.name ?? meta.element?.name]
      .filter(Boolean).join(' ') || 'the element'
    if (meta.actionOk !== true) {
      return { text: `Could not ${meta.elementAction} ${target}: ${meta.error ?? 'unknown reason'}` }
    }
    if (meta.elementAction === 'describe') {
      const bits = [`${target} in "${meta.window}"`]
      if (resolved.id) bits.push(`aid=${resolved.id}`)
      const actionNames = Array.isArray(meta.actions) ? meta.actions : meta.available
      if (Array.isArray(actionNames) && actionNames.length > 0) {
        bits.push(`actions: ${actionNames.join(', ')}`)
      } else {
        bits.push('advertises no action')
      }
      if (Array.isArray(meta.states) && meta.states.length > 0) bits.push(`states: ${meta.states.join(', ')}`)
      if (Array.isArray(meta.rect)) bits.push(`screen rect ${meta.rect.join(',')}`)
      return { text: `${bits.join('; ')}.` }
    }
    const parts = [`${meta.elementAction} on ${target} in "${meta.window}"`]
    if (meta.value !== undefined && meta.value !== null) parts.push(`value=${JSON.stringify(meta.value)}`)
    if (Array.isArray(meta.statesAfter) && meta.statesAfter.length > 0) {
      parts.push(`state now: ${meta.statesAfter.join(', ')}`)
    }
    if (meta.focusRestored === true) parts.push('focus was handed back to the previously focused window')
    return { text: `${parts.join('; ')}.` }
  },
}

const windowTool = {
  name: 'screen_window',
  description: 'Focus one window, optionally click or type inside it, then capture just that window as an '
    + 'image. Click coordinates are fractions of the WINDOW rectangle (0,0 is its top-left), not of the '
    + 'desktop. X11 cannot read a covered window\'s own surface, so the window is raised first and anything '
    + 'that was covering it is not part of the result; focus is handed back afterwards unless keepFocus is false.',
  parameters: {
    type: 'object',
    properties: {
      window: WINDOW_PARAM,
      focus: { type: 'boolean', description: 'Raise the window first; defaults to true.' },
      nx: { type: 'number', description: 'Horizontal position of a click, as a fraction of the WINDOW width.' },
      ny: { type: 'number', description: 'Vertical position of a click, as a fraction of the WINDOW height.' },
      click: { type: 'boolean', description: 'Click at nx/ny before capturing; defaults to false.' },
      keys: KEY_PARAM,
      text: { type: 'string', description: 'Text to type into the window before capturing.' },
      enter: { type: 'boolean', description: 'Press Enter after any text.' },
      keepFocus: { type: 'boolean', description: 'Hand focus back to the previously focused window after capturing; defaults to true.' },
    },
    required: ['window'],
    additionalProperties: false,
  },
  output: SCREENSHOT_OUTPUT,
  async execute(args, exec, ctx) {
    const request = { action: 'window', window: args.window, inline: true }
    for (const key of ['focus', 'nx', 'ny', 'click', 'keys', 'text', 'enter', 'keepFocus']) {
      if (args[key] !== undefined) request[key] = args[key]
    }
    const meta = await runSidecar(request, exec?.signal)

    const acted = meta.acted ?? {}
    const parts = [
      `Window "${meta.window}" (${meta.windowId}), ${meta.windowWidth}x${meta.windowHeight},`,
      meta.focused === true ? 'brought to the front.' : 'could NOT be brought to the front.',
    ]
    if (acted.clickedAt) parts.push(`Clicked at window fraction (${acted.clickedAt.nx},${acted.clickedAt.ny}).`)
    if (Array.isArray(acted.keys) && acted.keys.length > 0) {
      parts.push(`Pressed ${acted.keys.map((combo) => `"${combo}"`).join(', ')}.`)
    }
    if (acted.text) parts.push(`Typed ${acted.text.characters} character(s) via ${acted.text.method}.`)
    if (acted.enter === true) parts.push('Pressed Enter.')
    parts.push(`Captured from the desktop after raising it, ${meta.cropWidth}x${meta.cropHeight} px, `
      + (meta.lossless === true ? 'full native resolution, nothing lost.' : `downscaled x${round(meta.scale)}.`))
    parts.push(String(meta.occlusionNote ?? ''))
    parts.push('Click inside this image with screen_window nx/ny as fractions of the WINDOW rectangle. '
      + 'To click with screen_click instead, convert to desktop fractions first.')
    if (meta.focusRestored === true) parts.push('Focus was handed back to the previously focused window.')
    return await imageFromMeta(ctx, exec, meta, parts.join(' '), 'window.png')
  },
}

// ---------------------------------------------------------------------------
// Registration
// ---------------------------------------------------------------------------

export function apply(ctx) {
  // Warm the interpreter probe in the background so the first tool call does
  // not pay for it.
  void ensurePython().catch(() => {})

  const tools = [
    [lookTool, 'screen_look'],
    [zoomTool, 'screen_zoom'],
    [windowsTool, 'screen_windows'],
    [moveTool, 'screen_move'],
    [clickTool, 'screen_click'],
    [keyTool, 'screen_key'],
    [typeTool, 'screen_type'],
    [waitTool, 'screen_wait'],
    [elementsTool, 'screen_elements'],
    [actTool, 'screen_act'],
    [windowTool, 'screen_window'],
  ]
  for (const [tool, label] of tools) {
    // Every registration rides ctx.effect so a fiber dispose (hot reload,
    // uninject) unregisters it with no residue.
    ctx.effect(() => ctx.tools.register({
      ...tool,
      execute: (args, exec) => tool.execute(args, exec, ctx),
    }), `dsh-screen-agent-linux: ${label}`)
  }
}
