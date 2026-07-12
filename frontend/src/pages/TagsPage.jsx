import { Link } from 'react-router-dom'
import Shell from '../components/Shell'
import TagsSection from '../components/TagsSection'

/** Tag management gets its own page — with a real library this list is
 * hundreds of rows, far too big to live inside Settings. */
export default function TagsPage() {
  return (
    <Shell>
      <div className="library">
        <h1 className="page-title">Tags</h1>
        <p className="settings-help">
          Click a name to rename it. Colors tint chips and sidebar dots;
          hierarchy changes apply everywhere instantly.{' '}
          <Link to="/settings">Back to Settings.</Link>
        </p>
        <TagsSection standalone />
      </div>
    </Shell>
  )
}
