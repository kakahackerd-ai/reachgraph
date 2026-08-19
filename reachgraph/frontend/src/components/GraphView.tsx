import { useEffect, useMemo, useRef, useState } from 'react'
import ForceGraph3D, { type ForceGraphMethods } from 'react-force-graph-3d'
import type { GraphData, GraphEdge, GraphNode } from '../lib/types'

const LABEL_COLOR: Record<string, string> = {
  Package: '#22d3ee',
  Version: '#22d3ee',
  Application: '#a78bfa',
  File: '#fb923c',
}

const DIM_COLOR = 'rgba(148, 163, 184, 0.25)'
const SOURCE_COLOR = '#f8fafc'

function endpointId(endpoint: unknown): string {
  if (endpoint && typeof endpoint === 'object' && 'id' in endpoint) {
    return String((endpoint as { id: unknown }).id)
  }
  return String(endpoint)
}

interface RfNode {
  id: string
  key: string
  label: string
  depth: number
}

interface RfLink {
  source: string
  target: string
}

export interface GraphViewProps {
  graph: GraphData
  sourceKey?: string
  /** Node keys to visually emphasize (e.g. a selected dependency's blast radius). Empty = show everything at full opacity. */
  highlightKeys?: Set<string>
  onNodeClick?: (key: string) => void
  height?: number
}

export default function GraphView({ graph, sourceKey, highlightKeys, onNodeClick, height }: GraphViewProps) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const fgRef = useRef<ForceGraphMethods<RfNode, RfLink> | undefined>(undefined)
  const [size, setSize] = useState({ width: 800, height: height ?? 520 })

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0]
      if (!entry) return
      setSize({ width: entry.contentRect.width, height: height ?? Math.max(420, entry.contentRect.height) })
    })
    observer.observe(el)
    return () => observer.disconnect()
  }, [height])

  const data = useMemo(() => {
    const nodes: RfNode[] = graph.nodes.map((n: GraphNode) => ({ id: n.key, key: n.key, label: n.label, depth: n.depth }))
    const links: RfLink[] = graph.edges.map((e: GraphEdge) => ({ source: e.source, target: e.target }))
    return { nodes, links }
  }, [graph])

  useEffect(() => {
    const t = setTimeout(() => fgRef.current?.zoomToFit(600, 60), 350)
    return () => clearTimeout(t)
  }, [data])

  const emphasized = highlightKeys && highlightKeys.size > 0

  return (
    <div ref={containerRef} style={{ width: '100%', height: height ?? '100%', position: 'relative' }}>
      <ForceGraph3D
        ref={fgRef}
        graphData={data}
        width={size.width}
        height={size.height}
        backgroundColor="rgba(0,0,0,0)"
        showNavInfo={false}
        nodeRelSize={4.5}
        nodeLabel={(n) => `<div class="graph-tooltip">${n.key}</div>`}
        nodeVal={(n) => (n.key === sourceKey ? 3.2 : 1.4)}
        nodeColor={(n) => {
          if (n.key === sourceKey) return SOURCE_COLOR
          if (emphasized && !highlightKeys!.has(n.key)) return DIM_COLOR
          return LABEL_COLOR[n.label] ?? '#64748b'
        }}
        nodeOpacity={0.95}
        linkColor={(l) => {
          const s = endpointId(l.source)
          const t = endpointId(l.target)
          if (emphasized && (!highlightKeys!.has(s) || !highlightKeys!.has(t))) {
            return 'rgba(100, 116, 139, 0.12)'
          }
          return 'rgba(34, 211, 238, 0.35)'
        }}
        linkWidth={0.6}
        linkDirectionalParticles={emphasized ? 2 : 0}
        linkDirectionalParticleWidth={1.6}
        linkDirectionalParticleColor={() => '#fb923c'}
        onNodeClick={(n) => onNodeClick?.(n.key)}
        enableNodeDrag={false}
      />
    </div>
  )
}
