import { useMemo, useRef } from 'react'
import { Canvas, useFrame } from '@react-three/fiber'
import * as THREE from 'three'

function Network() {
  const group = useRef<THREE.Group>(null)

  const { positions, linePositions, colors } = useMemo(() => {
    const count = 90
    const radius = 9
    const pts: THREE.Vector3[] = []
    for (let i = 0; i < count; i++) {
      const v = new THREE.Vector3(
        (Math.random() - 0.5) * radius * 2,
        (Math.random() - 0.5) * radius * 1.2,
        (Math.random() - 0.5) * radius * 2,
      )
      pts.push(v)
    }

    const positions = new Float32Array(count * 3)
    pts.forEach((p, i) => p.toArray(positions, i * 3))

    const palette = [
      new THREE.Color('#5b8fef'),
      new THREE.Color('#9c8cf0'),
      new THREE.Color('#e0ab52'),
      new THREE.Color('#ff6a3d'),
    ]
    const colors = new Float32Array(count * 3)
    pts.forEach((_, i) => {
      const c = palette[i % palette.length]
      c.toArray(colors, i * 3)
    })

    const lineSegs: number[] = []
    for (let i = 0; i < count; i++) {
      let nearest = -1
      let nearestDist = Infinity
      for (let j = 0; j < count; j++) {
        if (i === j) continue
        const d = pts[i].distanceTo(pts[j])
        if (d < nearestDist) {
          nearestDist = d
          nearest = j
        }
      }
      if (nearest >= 0 && nearestDist < radius * 0.9) {
        lineSegs.push(pts[i].x, pts[i].y, pts[i].z, pts[nearest].x, pts[nearest].y, pts[nearest].z)
      }
    }

    return { positions, linePositions: new Float32Array(lineSegs), colors }
  }, [])

  useFrame((_, delta) => {
    if (group.current) group.current.rotation.y += delta * 0.045
  })

  return (
    <group ref={group}>
      <points>
        <bufferGeometry>
          <bufferAttribute attach="attributes-position" args={[positions, 3]} />
          <bufferAttribute attach="attributes-color" args={[colors, 3]} />
        </bufferGeometry>
        <pointsMaterial size={0.11} vertexColors sizeAttenuation transparent opacity={0.9} />
      </points>
      <lineSegments>
        <bufferGeometry>
          <bufferAttribute attach="attributes-position" args={[linePositions, 3]} />
        </bufferGeometry>
        <lineBasicMaterial color="#ff6a3d" transparent opacity={0.1} />
      </lineSegments>
    </group>
  )
}

export default function AmbientNetwork() {
  return (
    <Canvas
      camera={{ position: [0, 0, 14], fov: 55 }}
      style={{ position: 'fixed', inset: 0, zIndex: 0 }}
      gl={{ antialias: true, alpha: true }}
    >
      <ambientLight intensity={1.2} />
      <Network />
    </Canvas>
  )
}
