# ShareInput 应用图标

采用用户选定的 A 方案：蓝色圆角底、双屏互联与双向箭头。
使用内置 image_gen 生成提案和透明底正式母版；未使用 CLI/API fallback。
`app-icon-master.png` 为 RGBA 母版，`app-icon.ico` 含 16–256 像素尺寸，
`app-icon.icns` 含 macOS 标准与 Retina 尺寸。
运行时使用 `src/assets/app-icon.png`（256 像素），避免加载大母版。
ICO/ICNS 由母版做格式转换生成，PNG 用系统 sips 缩放。

选定方案生成提示词：

> Use case: logo-brand. Asset: preview-only app icon proposal for Share Input, a cross-platform utility for remote desktop, keyboard and mouse sharing, clipboard sync and device connections. Create a single polished macOS/Windows app icon centered on a neutral very pale gray background with generous margin. Large rounded square tile, blue and cyan palette that fits a restrained white/blue desktop UI. Refined soft depth and gentle highlights, sharp simple geometry, readable at 32px, premium professional utility, not a game. No text, no letters, no watermark, no mockup device, no tiny details. Distinct concept A: two interlocking softly rounded screen outlines, one offset behind the other, connected by a continuous bidirectional link. Deep royal blue tile, clear white front screen and cyan rear screen, coherent simple bold silhouette. Convey multiple computers working as one, subtle layered depth.

透明母版编辑提示词：

> Use case: logo-brand; production app icon edit. Edit the provided selected ShareInput app icon. Preserve the exact two interlocking monitor silhouettes, the white left monitor, cyan upper-right monitor, white rightward arrow and white leftward arrow, their shapes and balance, and the vivid deep blue rounded-square tile. Remove the entire pale gray outer background and all external cast shadow. Outside the blue rounded-square tile must be genuinely transparent with clean alpha edges. Keep delicate internal depth on the blue tile, reduce wide surrounding margin: tile nearly fills the canvas with about 6% transparent padding. Square production app icon, crisp at small sizes, no added text or extra elements. This is the final master icon, not a mockup or presentation sheet.

最终边缘整理提示词：

> Production icon cleanup, use case logo-brand. Keep the exact current blue rounded-square tile and the white/cyan interlocking computer screens with two white arrows. Fix only the outer alpha edge: remove every stray blue speck, hairline, artifact and fleck outside the tile. The boundary must be a clean smooth geometrically regular rounded square with continuous antialiasing and fully transparent outer canvas. No shadows outside, no stroke around the tile, no changes to screens/arrows/colors, no added elements or text. This must look like a carefully exported vector icon on true transparency, not an extracted photo.
