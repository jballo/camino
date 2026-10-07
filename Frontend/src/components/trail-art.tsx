import type { CSSProperties, ReactNode } from "react";

// Pixel-art pieces for the home page "camino" motif. Each sprite is a grid of
// rows where "#" is a filled cell, rendered as crisp SVG rects.

const LLAMA = [
  "......#.",
  "......##",
  "......#.",
  "......#.",
  "#######.",
  ".######.",
  ".##..##.",
  ".#....#.",
];

const CHAKANA = [
  "...##...",
  "..####..",
  ".######.",
  "###..###",
  "###..###",
  ".######.",
  "..####..",
  "...##...",
];

const STAIRS = [
  "........",
  "....####",
  "....#...",
  "...#####",
  "...#....",
  ".#######",
  ".#......",
  "########",
];

const WEAVE = [
  "......##",
  ".##...##",
  ".##.##..",
  "....##..",
  "..##....",
  "..##.##.",
  "##...##.",
  "##......",
];

const FLAG = [
  "#.......",
  "#####...",
  "######..",
  "#####...",
  "#.......",
  "#.......",
  "#.......",
  "#.......",
];

function Sprite({
  rows,
  size,
  className,
  style,
}: {
  rows: string[];
  size: number;
  className?: string;
  style?: CSSProperties;
}) {
  const width = rows[0].length;
  return (
    <svg
      aria-hidden="true"
      viewBox={`0 0 ${width} ${rows.length}`}
      width={size}
      height={(size * rows.length) / width}
      shapeRendering="crispEdges"
      className={className}
      style={style}
      fill="currentColor"
    >
      {rows.flatMap((row, y) =>
        [...row].map((cell, x) =>
          cell === "#" ? (
            <rect key={`${x}-${y}`} x={x} y={y} width={1.02} height={1.02} />
          ) : null,
        ),
      )}
    </svg>
  );
}

export function Llama({
  size,
  className,
  style,
}: {
  size: number;
  className?: string;
  style?: CSSProperties;
}) {
  return <Sprite rows={LLAMA} size={size} className={className} style={style} />;
}

export function Flag({ size, className }: { size: number; className?: string }) {
  return <Sprite rows={FLAG} size={size} className={className} />;
}

/** Stepped-cross outline used as the eyebrow mark. */
export function ChakanaMark({ className }: { className?: string }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="-0.75 -0.75 15.5 15.5"
      width={14.5}
      height={14.5}
      className={className}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      shapeRendering="crispEdges"
    >
      <path d="M4.5 0h5v2H12v2h2v6h-2v2H9.5v2h-5v-2H2v-2H0V4h2V2h2.5z" />
    </svg>
  );
}

/** Hand-drawn double stroke under the hero headline. */
export function Scribble({ className }: { className?: string }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 304 26"
      width={304}
      height={26}
      className={className}
      fill="none"
      stroke="currentColor"
      strokeWidth={3.75}
      strokeLinecap="round"
    >
      <path d="M4 14.4C44 11.4 96 11.4 140 13.4S214 14.4 240 10.6 284 6.4 298 13.6" />
      <path d="M22 22.4C60 20.4 118 20.6 166 21.6S212 20.8 226 19.8" />
    </svg>
  );
}

function SparkleGlyph() {
  return (
    <svg aria-hidden="true" viewBox="0 0 84 84" className="absolute inset-0 size-full" fill="currentColor" shapeRendering="crispEdges">
      <rect x="36" y="37" width="12" height="11" />
      <rect x="39" y="19" width="6" height="12" />
      <rect x="40" y="54" width="6" height="12" />
      <rect x="19" y="40" width="12" height="6" />
      <rect x="54" y="40" width="12" height="6" />
      <rect x="25" y="25" width="6" height="6" />
      <rect x="54" y="24" width="6" height="7" />
      <rect x="25" y="54" width="6" height="6" />
      <rect x="55" y="54" width="6" height="6" />
    </svg>
  );
}

type Tile = {
  size: number;
  right: number;
  top: number;
  rotate: number;
  radius: number;
  accent?: boolean;
  glyph: ReactNode;
};

const TILES: Tile[] = [
  {
    size: 112,
    right: 120,
    top: 69.5,
    rotate: 5.9,
    radius: 9,
    glyph: <Sprite rows={LLAMA} size={64} />,
  },
  {
    size: 84,
    right: 29.5,
    top: 46,
    rotate: -8,
    radius: 8,
    glyph: <SparkleGlyph />,
  },
  {
    size: 91,
    right: 14.5,
    top: 150.5,
    rotate: -4.8,
    radius: 8,
    accent: true,
    glyph: <Sprite rows={CHAKANA} size={50} />,
  },
  {
    size: 90,
    right: 150,
    top: 212,
    rotate: -7.8,
    radius: 8,
    glyph: <Sprite rows={STAIRS} size={52} />,
  },
  {
    size: 74,
    right: 53,
    top: 265,
    rotate: 3.5,
    radius: 8,
    glyph: <Sprite rows={WEAVE} size={42} />,
  },
];

/** Scattered pixel tiles on the right of the hero. */
export function TileCluster({ className }: { className?: string }) {
  return (
    <div aria-hidden="true" className={className}>
      {TILES.map((tile, index) => (
        <div
          key={index}
          className={`absolute flex items-center justify-center ${
            tile.accent
              ? "bg-brand-accent text-background"
              : "border-[1.5px] border-tile-border bg-tile text-muted-foreground"
          }`}
          style={{
            width: tile.size,
            height: tile.size,
            right: tile.right,
            top: tile.top,
            borderRadius: tile.radius,
            transform: `rotate(${tile.rotate}deg)`,
            boxShadow: "0 4.5px 0 rgb(0 0 0 / .38)",
          }}
        >
          {tile.glyph}
        </div>
      ))}
    </div>
  );
}

const RAIL_LLAMAS = [
  { left: 92.5, size: 32, className: "text-[#484b4e]" },
  { left: 233.5, size: 42, className: "text-secondary" },
  { left: 384, size: 30, className: "text-[#484b4e]" },
  { left: 522, size: 42, className: "text-secondary" },
  { left: 672.5, size: 32, className: "text-[#484b4e]" },
  { left: 813.5, size: 42, className: "text-brand-accent" },
];

/** A caravan of llamas walking a railway track. `bob` makes them step in place. */
export function LlamaTrail({ className, bob = false }: { className?: string; bob?: boolean }) {
  return (
    <div aria-hidden="true" className={`relative h-[61.5px] ${className ?? ""}`}>
      {RAIL_LLAMAS.map((llama, index) => (
        <Llama
          key={llama.left}
          size={llama.size}
          className={`absolute bottom-[20px] ${llama.className} ${bob ? "llama-bob" : ""}`}
          style={{
            left: `${(llama.left / 948) * 100}%`,
            animationDelay: bob && index % 2 ? "0.6s" : undefined,
          }}
        />
      ))}
      <div
        className="absolute inset-x-0 bottom-0 h-[12.75px] border-y-[2.25px] border-rail"
        style={{
          backgroundImage:
            "repeating-linear-gradient(90deg, transparent 0 9.75px, var(--rail-tie) 9.75px 11.25px, transparent 11.25px 21px)",
        }}
      />
    </div>
  );
}
