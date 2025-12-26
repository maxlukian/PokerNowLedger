export default async function handler(req, res) {
    const { gameId } = req.query;

    if (!gameId || !gameId.match(/^pgl[a-zA-Z0-9_-]+$/)) {
        return res.status(400).json({ error: 'Invalid game ID' });
    }

    try {
        const response = await fetch(`https://www.pokernow.club/games/${gameId}/players_sessions`);
        
        if (!response.ok) {
            return res.status(response.status).json({ error: 'Failed to fetch from PokerNow' });
        }

        const data = await response.json();
        res.status(200).json(data);
    } catch (err) {
        res.status(500).json({ error: err.message });
    }
}