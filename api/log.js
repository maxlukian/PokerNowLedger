export default async function handler(req, res) {
    const { gameId, before_at } = req.query;

    if (!gameId || !gameId.match(/^pgl[a-zA-Z0-9_-]+$/)) {
        return res.status(400).json({ error: 'Invalid game ID' });
    }

    try {
        let url = `https://www.pokernow.com/games/${gameId}/log`;
        if (before_at) url += `?before_at=${encodeURIComponent(before_at)}`;

        const response = await fetch(url);

        if (!response.ok) {
            return res.status(response.status).json({ error: 'Failed to fetch log' });
        }

        const data = await response.json();
        res.status(200).json(data);
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
}
