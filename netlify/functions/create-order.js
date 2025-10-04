const { google } = require('googleapis');

const SHEET_ID = '1_Z9hvD6GvNEn0LcD60Ajwh8NWLm07Xp_b4Aqub1G3os';

exports.handler = async (event) => {
  if (event.httpMethod !== 'POST') {
    return {
      statusCode: 405,
      body: JSON.stringify({ error: 'Method not allowed' })
    };
  }

  try {
    const data = JSON.parse(event.body);
    const { email, produits, montant, idTransaction, modePaiement } = data;

    const credentials = JSON.parse(process.env.GOOGLE_CREDENTIALS);
    
    const auth = new google.auth.GoogleAuth({
      credentials: credentials,
      scopes: ['https://www.googleapis.com/auth/spreadsheets']
    });

    const sheets = google.sheets({ version: 'v4', auth });

    const now = new Date().toISOString();
    const newRow = [
      '',
      email,
      JSON.stringify(produits),
      montant,
      'En attente',
      idTransaction,
      modePaiement,
      now,
      '',
      ''
    ];

    await sheets.spreadsheets.values.append({
      spreadsheetId: SHEET_ID,
      range: 'Commandes!A:J',
      valueInputOption: 'USER_ENTERED',
      resource: {
        values: [newRow]
      }
    });

    return {
      statusCode: 200,
      headers: {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'Content-Type'
      },
      body: JSON.stringify({ 
        success: true, 
        message: 'Commande créée avec succès' 
      })
    };

  } catch (error) {
    console.error('Erreur:', error);
    return {
      statusCode: 500,
      body: JSON.stringify({ 
        error: 'Erreur lors de la création de la commande',
        details: error.message 
      })
    };
  }
};
Add create-order function
