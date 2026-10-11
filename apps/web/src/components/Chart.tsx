import { motionOk, useTheme } from "@repo/ui";
import { BarChart, CustomChart, LineChart, ScatterChart } from "echarts/charts";
import {
  AriaComponent,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  MarkPointComponent,
  TooltipComponent,
} from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useRef } from "react";

echarts.use([
  LineChart,
  ScatterChart,
  BarChart,
  CustomChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  MarkLineComponent,
  MarkAreaComponent,
  MarkPointComponent,
  DataZoomComponent,
  AriaComponent,
  CanvasRenderer,
]);

export type EOption = echarts.EChartsCoreOption;
export { echarts };

/** A design-token colour as the current theme defines it (charts draw on canvas, so they
 *  need the value, not the CSS variable). */
export function token(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(`--${name}`).trim() || "#888";
}

/** Axis, grid and text styling shared by every chart, from the theme. */
export function base(): EOption {
  const fg = token("text");
  const muted = token("muted");
  const line = token("border");
  const font = "IBM Plex Sans, system-ui, sans-serif";
  const axis = {
    axisLine: { lineStyle: { color: line } },
    axisTick: { lineStyle: { color: line } },
    axisLabel: { color: muted, fontFamily: "IBM Plex Mono, monospace", fontSize: 11 },
    splitLine: { lineStyle: { color: line, opacity: 0.6 } },
    nameTextStyle: { color: muted, fontFamily: font, fontSize: 12 },
  };
  return {
    animation: motionOk(),
    textStyle: { fontFamily: font, color: fg },
    aria: { enabled: false }, // the container carries the label; tables give the data
    tooltip: {
      backgroundColor: token("surface"),
      borderColor: token("border-strong"),
      textStyle: { color: fg, fontFamily: font, fontSize: 13 },
      confine: true,
    },
    legend: { textStyle: { color: fg, fontSize: 12 }, itemHeight: 10 },
    xAxis: axis,
    yAxis: axis,
  };
}

/** ECharts on a canvas sized to its box, re-rendered on theme change, resized with the box.
 *  `label` is the accessible name; pass a table or text alternative next to the chart. */
export function Chart({
  option,
  height,
  label,
  onClick,
  className,
  testId,
}: {
  option: (b: EOption) => EOption;
  height: number | string;
  label: string;
  onClick?: (p: echarts.ECElementEvent) => void;
  className?: string;
  testId?: string;
}) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const [theme] = useTheme();
  const click = useRef(onClick);
  click.current = onClick;

  useEffect(() => {
    if (!el.current) return;
    const c = echarts.init(el.current, undefined, { renderer: "canvas" });
    chart.current = c;
    c.on("click", (p) => click.current?.(p as echarts.ECElementEvent));
    const ro = new ResizeObserver(() => c.resize());
    ro.observe(el.current);
    return () => {
      ro.disconnect();
      c.dispose();
      chart.current = null;
    };
  }, []);

  useEffect(() => {
    const b = base();
    chart.current?.setOption(option(b), { notMerge: true, lazyUpdate: true });
  }, [option, theme]);

  return <div ref={el} role="img" aria-label={label} data-testid={testId} className={className} style={{ height, width: "100%" }} />;
}
