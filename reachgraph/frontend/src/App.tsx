import { Route, Routes } from 'react-router-dom'
import Shell from './components/Shell'
import IntroScreen from './screens/IntroScreen'
import PackageScreen from './screens/PackageScreen'
import RepoScreen from './screens/RepoScreen'

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<IntroScreen />} />
      <Route
        path="/npm"
        element={
          <Shell>
            <PackageScreen />
          </Shell>
        }
      />
      <Route
        path="/repo"
        element={
          <Shell>
            <RepoScreen />
          </Shell>
        }
      />
    </Routes>
  )
}
